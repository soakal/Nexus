import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# A single step that has been attempted this many times (across retry + replan)
# without succeeding is declared exhausted — the task finalizes 'failed' with
# step_exhausted rather than looping forever on a poison step. attempts is
# incremented by _mark_step_running and PRESERVED across _patch_step_durably.
MAX_STEP_ATTEMPTS = 5


def _idem_key(task_id: int, step_index: int, prompt: str) -> str:
    return hashlib.sha256(f"{task_id}:{step_index}:{prompt}".encode()).hexdigest()[:16]


@dataclass
class Step:
    index: int
    prompt: str
    description: str = ""


@dataclass
class Plan:
    task_prompt: str
    steps: list = field(default_factory=list)

    def patch_step(self, step: Step, new_prompt: str) -> None:
        for i, s in enumerate(self.steps):
            if s.index == step.index:
                self.steps[i] = Step(index=s.index, prompt=new_prompt, description=s.description)
                return


@dataclass
class TaskResult:
    success: bool
    output: list = field(default_factory=list)
    plan: Plan | None = None
    reason: str = ""


def _is_failure(result: str) -> bool:
    """Decide whether an executor result is a genuine failure.

    A result is only a failure when it is empty/whitespace, or when it is an
    outright refusal that contains NO substantive answer. A response that says
    "based on my training knowledge the latest version is X" is a SUCCESS — the
    executor did its job using available knowledge.
    """
    if not result or not result.strip():
        return True

    text = result.strip()

    # Pure refusals — only treat as failure if the response is short enough that
    # there is clearly no real answer attached. A long answer that merely opens
    # with a caveat ("I can't fetch live data, but based on training...") is fine.
    hard_refusals = [
        "i cannot help with",
        "i'm unable to assist",
        "i am unable to assist",
        "as an ai language model, i cannot",
        "i refuse to",
        "i can't help with that",
        "i cannot complete this task",
        "i'm not able to complete",
    ]
    low = text.lower()
    for r in hard_refusals:
        if r in low and len(text) < 400:
            return True

    # Bare "I don't have access to real-time data" with nothing else useful.
    no_data_markers = [
        "i don't have access to real-time",
        "i do not have access to real-time",
        "i don't have real-time access",
        "i cannot access real-time",
        "i can't access the internet",
        "i don't have the ability to browse",
    ]
    for m in no_data_markers:
        # Only a failure if that's essentially the WHOLE response (no fallback answer).
        if m in low and len(text) < 250:
            return True

    return False


async def _opus_plan(task_prompt: str, learning: str = "") -> Plan:
    from backend.agents.router import opus
    from backend.config import get_settings

    if get_settings().agent_write_enabled:
        from backend.agents.write_tools import all_planner_block
        tool_block = all_planner_block()
    else:
        from backend.agents.tools import planner_tool_block
        tool_block = planner_tool_block()

    learning_block = ""
    if learning:
        learning_block = f"\nPRIOR ATTEMPTS THAT FAILED (avoid repeating these mistakes):\n{learning}\n"

    plan_prompt = f"""Decompose this task into numbered execution steps for an executor agent.

TASK: {task_prompt}

THE EXECUTOR HAS THESE TOOLS (it calls them natively — do NOT prefix steps):
{tool_block}

Some tools perform real actions (home_control, hermes_command); they are safety-gated and risky ones need human confirmation — use them only when the task clearly asks to change something.
For tasks answerable from general knowledge alone, the executor just answers directly.
Keep it minimal: usually 1-3 steps. The final step synthesizes the answer.
{learning_block}
Return JSON only:
{{
  "steps": [
    {{"index": 1, "description": "step description", "prompt": "exact prompt for executor"}},
    ...
  ]
}}

Each step must be atomic and runnable on its own. Maximum 10 steps."""

    raw = await opus(plan_prompt)
    # Extract JSON from response
    start = raw.find("{")
    end = raw.rfind("}") + 1
    data = json.loads(raw[start:end])

    plan = Plan(task_prompt=task_prompt)
    for s in data.get("steps", []):
        plan.steps.append(Step(index=s["index"], prompt=s["prompt"], description=s.get("description", "")))
    return plan


async def _sonnet_execute(step: Step, context: list, *, task_id=None, task_start=None) -> str:
    from backend.agents import router
    from backend.config import get_settings

    if get_settings().agent_write_enabled:
        from backend.agents.write_tools import all_tool_specs, all_dispatchers
        specs, dispatch = all_tool_specs(), all_dispatchers()
    else:
        from backend.agents.tools import tool_specs, dispatcher_map
        specs, dispatch = tool_specs(), dispatcher_map()

    context_str = "\n".join([f"Step {i+1} result: {r}" for i, r in enumerate(context)]) if context else "No prior context."
    full_prompt = f"""Previous results:
{context_str}

Current task:
{step.prompt}

Execute this task and return the result directly.

You have tools available: call them to pull live homelab status, search
the web for current/real-time information, search the user's Obsidian vault when
the task needs that live data, or perform safe write actions (home_control,
hermes_command) when the task clearly asks to change something. If the task is
answerable from general knowledge alone, just answer directly without calling a
tool. Always produce a useful, substantive answer."""
    return await router.run_with_tools(
        model=router.SONNET_MODEL,
        max_tokens=8192,
        prompt=full_prompt,
        system="",
        tool_specs=specs,
        dispatch=dispatch,
        web_search=True,
        label="orchestrator_execute",
        task_id=task_id,
        task_start=task_start,
    )


async def _opus_debug(task: str, plan: Plan, failed_step: tuple, prior_results: list) -> dict:
    from backend.agents.router import opus
    step, result = failed_step
    debug_prompt = f"""A task execution step has failed. Analyze and provide a fix.

ORIGINAL TASK: {task}

PLAN:
{json.dumps([{"index": s.index, "description": s.description} for s in plan.steps], indent=2)}

FAILED STEP {step.index}: {step.prompt}

FAILURE OUTPUT: {result}

PRIOR SUCCESSFUL RESULTS:
{json.dumps(prior_results, indent=2)}

Return JSON only:
{{
  "action": "RETRY_STEP" | "REPLAN" | "ABORT",
  "reason": "explanation",
  "new_prompt": "revised prompt if RETRY_STEP",
  "new_plan": {{"steps": [...]}} // if REPLAN
}}"""
    raw = await opus(debug_prompt)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    return json.loads(raw[start:end])


async def _opus_verify(task_prompt: str, final_output: list, plan: "Plan", *, task_id=None, task_start=None) -> dict:
    """Run the Opus verifier over a completed task's output.

    Asks Opus to judge whether the task was GENUINELY accomplished. When the
    output makes a checkable claim about live homelab/vault/web state, Opus is
    instructed to call the available read-only tools rather than trust the text.

    Returns a dict with keys: verdict, confidence, reason, grounded, evidence.
    On ANY parse failure, returns a safe permissive default (verdict="uncertain")
    so a verifier crash NEVER destroys a genuinely-good completed task.

    BudgetExceeded and TaskAborted are NOT caught here — they propagate to the
    run_task try/except which already handles them correctly.

    Sync DB helpers are NOT called here — call _record_task_outcome via
    asyncio.to_thread from the caller (run_task durable path).
    """
    from backend.agents import router
    from backend.agents.tools import dispatcher_map, tool_specs

    _SAFE_DEFAULT = {
        "verdict": "uncertain",
        "confidence": 0.0,
        "reason": "verify_unparseable",
        "grounded": False,
        "evidence": None,
    }

    try:
        output_text = json.dumps(final_output)
        if len(output_text) > 4000:
            output_text = output_text[:4000] + "\n...[truncated]"

        step_descriptions = [
            f"  Step {s.index}: {s.description or s.prompt[:80]}"
            for s in plan.steps
        ]
        steps_block = "\n".join(step_descriptions) if step_descriptions else "  (no steps)"

        verify_prompt = f"""You are verifying whether a task was genuinely accomplished.

ORIGINAL TASK:
{task_prompt}

EXECUTED PLAN (steps that ran):
{steps_block}

FINAL OUTPUT (from the executor):
{output_text}

Your job: judge whether the task was GENUINELY accomplished based on this output.

If the output makes a checkable claim about live homelab state, vault notes, or
real-time web information, CALL the available read-only tools to verify the claim
rather than trusting the text alone. Set "grounded": true only if you actually
ran a tool check that supported the verdict.

Respond with JSON only — no prose before or after:
{{
  "verdict": "success" | "failure" | "partial" | "uncertain",
  "confidence": 0.0-1.0,
  "reason": "one-sentence explanation",
  "grounded": true | false,
  "evidence": "short quote of what was checked, or null"
}}"""

        raw = await router.run_with_tools(
            model=router.OPUS_MODEL,
            max_tokens=2048,
            prompt=verify_prompt,
            system="",
            tool_specs=tool_specs(),
            dispatch=dispatcher_map(),
            web_search=False,
            label="orchestrator_verify",
            task_id=task_id,
            task_start=task_start,
        )

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return _SAFE_DEFAULT
        result = json.loads(raw[start:end])
        # Validate required keys present; fill missing with defaults.
        return {
            "verdict": str(result.get("verdict", "uncertain")),
            "confidence": float(result.get("confidence", 0.0)),
            "reason": str(result.get("reason", "")),
            "grounded": bool(result.get("grounded", False)),
            "evidence": result.get("evidence"),
        }
    except Exception as e:
        # BudgetExceeded and TaskAborted are subclasses of Exception too, but we
        # do NOT want to swallow them — re-raise them explicitly.
        from backend.agents.router import TaskAborted
        from backend.safety.governor import BudgetExceeded
        if isinstance(e, (BudgetExceeded, TaskAborted)):
            raise
        logger.warning(f"_opus_verify parse/call failed (using safe default): {e}")
        return _SAFE_DEFAULT


# ---------------------------------------------------------------------------
# Durable DB helpers — every one of these is SYNCHRONOUS and must only ever be
# invoked via `asyncio.to_thread`. They open and close their own Session inside
# the worker thread, and return plain dicts/values so NO ORM object or Session
# ever crosses an `await` (Windows ProactorEventLoop safety, see CLAUDE.md).
# ---------------------------------------------------------------------------

def _persist_plan(task_id: int, plan_steps: list) -> None:
    """Delete any existing TaskStep rows for the task and insert one pending
    row per planned step. Also stamps Task.plan_json + status=running.

    `plan_steps` is a list of (index, prompt, description) tuples — plain data,
    not ORM/Step objects.
    """
    from sqlmodel import Session, select

    from backend.database import Task, TaskStep, engine

    with Session(engine) as session:
        for row in session.exec(select(TaskStep).where(TaskStep.task_id == task_id)).all():
            session.delete(row)
        for index, prompt, description in plan_steps:
            session.add(TaskStep(
                task_id=task_id,
                step_index=index,
                prompt=prompt,
                description=description,
                status="pending",
                idempotency_key=_idem_key(task_id, index, prompt),
            ))
        t = session.get(Task, task_id)
        if t:
            t.plan_json = json.dumps([{"index": i, "description": d} for i, _p, d in plan_steps])
            t.status = "running"
            t.updated_at = datetime.utcnow()
        session.commit()


def _load_steps(task_id: int) -> list[dict]:
    """Return all TaskStep rows for a task ordered by step_index as plain dicts.

    A step left in 'running' state (process died mid-step) is reset to 'pending'
    here so it is re-executed on resume.
    """
    from sqlmodel import Session, select

    from backend.database import TaskStep, engine

    out: list[dict] = []
    with Session(engine) as session:
        rows = session.exec(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_index)
        ).all()
        dirty = False
        for r in rows:
            if r.status == "running":
                r.status = "pending"
                r.updated_at = datetime.utcnow()
                dirty = True
        if dirty:
            session.commit()
        for r in rows:
            out.append({
                "id": r.id,
                "task_id": r.task_id,
                "step_index": r.step_index,
                "prompt": r.prompt,
                "description": r.description,
                "status": r.status,
                "output_json": r.output_json,
                "attempts": r.attempts,
                "idempotency_key": r.idempotency_key,
            })
    return out


def _load_done_outputs(task_id: int) -> list[str]:
    """Decoded outputs of all `done` steps, ordered by step_index. This is the
    rebuilt context — the core fix for the reset-each-retry bug."""
    from sqlmodel import Session, select

    from backend.database import TaskStep, engine

    outputs: list[str] = []
    with Session(engine) as session:
        rows = session.exec(
            select(TaskStep)
            .where(TaskStep.task_id == task_id, TaskStep.status == "done")
            .order_by(TaskStep.step_index)
        ).all()
        for r in rows:
            if r.output_json is None:
                continue
            try:
                outputs.append(json.loads(r.output_json))
            except Exception:
                outputs.append(r.output_json)
    return outputs


def _mark_step_running(step_db_id: int) -> None:
    from sqlmodel import Session

    from backend.database import TaskStep, engine

    with Session(engine) as session:
        s = session.get(TaskStep, step_db_id)
        if s:
            s.status = "running"
            s.heartbeat_at = datetime.utcnow()
            s.attempts += 1
            s.updated_at = datetime.utcnow()
            session.commit()


def _complete_step(step_db_id: int, output: str) -> None:
    """THE CHECKPOINT — committed the instant a step finishes successfully."""
    from sqlmodel import Session

    from backend.database import TaskStep, engine

    with Session(engine) as session:
        s = session.get(TaskStep, step_db_id)
        if s:
            s.status = "done"
            s.output_json = json.dumps(output)
            s.updated_at = datetime.utcnow()
            session.commit()


def _fail_step(step_db_id: int, output: str) -> None:
    from sqlmodel import Session

    from backend.database import TaskStep, engine

    with Session(engine) as session:
        s = session.get(TaskStep, step_db_id)
        if s:
            s.status = "failed"
            s.output_json = json.dumps(output)
            s.updated_at = datetime.utcnow()
            session.commit()


def _is_cancel_requested(task_id: int) -> bool:
    from sqlmodel import Session

    from backend.database import Task, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        return bool(t and t.cancel_requested)


def _finalize_task(task_id: int, status: str, result_json: str | None) -> None:
    from sqlmodel import Session, select

    from backend.database import Task, TaskStep, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        if t:
            t.status = status
            if result_json is not None:
                t.result_json = result_json
            done = session.exec(
                select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.status == "done")
            ).all()
            t.steps_taken = len(done)
            t.updated_at = datetime.utcnow()
            session.commit()


def _record_agent_run(task_id: int, prompt: str, output: str, success: bool, elapsed_ms: int) -> None:
    from sqlmodel import Session

    from backend.database import AgentRun, engine

    with Session(engine) as session:
        session.add(AgentRun(
            task_id=task_id,
            agent_type="orchestrator",
            model="sonnet",
            prompt_snippet=prompt[:200],
            output_snippet=output[:200],
            success=success,
            duration_ms=elapsed_ms,
        ))
        session.commit()


def _record_task_outcome(task_id: int, verdict: str, confidence: float, reason: str, grounded: bool, evidence: str | None) -> None:
    """Insert one TaskOutcome row for a completed durable task (model="opus").

    Sync — must only be called via asyncio.to_thread. Opens and closes its own
    Session; returns plain None so no ORM object crosses an await.
    """
    from sqlmodel import Session

    from backend.database import TaskOutcome, engine

    with Session(engine) as session:
        session.add(TaskOutcome(
            task_id=task_id,
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            grounded=grounded,
            evidence=evidence,
            model="opus",
        ))
        session.commit()


def _load_learning_context(limit: int = 5) -> str:
    """Return a compact plain-text block of the most recent failed signals.

    Pulls (newest-first) failed TaskOutcome rows (reason) and failed AgentRun
    rows (prompt_snippet + output_snippet), combined and capped to `limit` items.
    Each entry is truncated to ~200 chars. Returns "" if nothing or on any error.

    Sync — must only be called via asyncio.to_thread.
    """
    try:
        from sqlmodel import Session, select

        from backend.database import AgentRun, TaskOutcome, engine

        items: list[str] = []
        with Session(engine) as session:
            # (a) recent failed TaskOutcome rows, newest-first.
            outcomes = session.exec(
                select(TaskOutcome)
                .where(TaskOutcome.verdict.in_(["failure", "partial"]))
                .order_by(TaskOutcome.created_at.desc())
                .limit(limit)
            ).all()
            for o in outcomes:
                snippet = (o.reason or "")[:200]
                items.append(f"- [failed] {snippet}")

            remaining = limit - len(items)
            if remaining > 0:
                # (b) recent failed AgentRun rows, newest-first.
                runs = session.exec(
                    select(AgentRun)
                    .where(AgentRun.success == False)  # noqa: E712
                    .order_by(AgentRun.created_at.desc())
                    .limit(remaining)
                ).all()
                for r in runs:
                    prompt_part = (r.prompt_snippet or "")[:100]
                    output_part = (r.output_snippet or "")[:100]
                    snippet = f"{prompt_part} -> {output_part}"[:200]
                    items.append(f"- [failed] {snippet}")

        return "\n".join(items)
    except Exception:
        return ""


def _patch_step_durably(task_id: int, step_index: int, new_prompt: str) -> None:
    """RETRY_STEP: rewrite one step's prompt, recompute its idempotency key, reset
    it to pending and clear its output. Attempts are preserved (not reset)."""
    from sqlmodel import Session, select

    from backend.database import TaskStep, engine

    with Session(engine) as session:
        s = session.exec(
            select(TaskStep).where(
                TaskStep.task_id == task_id, TaskStep.step_index == step_index
            )
        ).first()
        if s:
            s.prompt = new_prompt
            s.idempotency_key = _idem_key(task_id, step_index, new_prompt)
            s.status = "pending"
            s.output_json = None
            s.updated_at = datetime.utcnow()
            session.commit()


def _replan_durably(task_id: int, new_steps: list) -> int:
    """REPLAN: keep all `done` rows (with their indices), delete the rest, and
    append the new steps as pending, indexed continuing after max(done_index).

    `new_steps` is a list of (prompt, description) tuples. Returns the number of
    pending steps inserted.
    """
    from sqlmodel import Session, select

    from backend.database import Task, TaskStep, engine

    with Session(engine) as session:
        rows = session.exec(select(TaskStep).where(TaskStep.task_id == task_id)).all()
        max_done = 0
        for r in rows:
            if r.status == "done":
                max_done = max(max_done, r.step_index)
            else:
                session.delete(r)

        inserted = 0
        for offset, (prompt, description) in enumerate(new_steps, start=1):
            idx = max_done + offset
            session.add(TaskStep(
                task_id=task_id,
                step_index=idx,
                prompt=prompt,
                description=description,
                status="pending",
                idempotency_key=_idem_key(task_id, idx, prompt),
            ))
            inserted += 1

        # Refresh plan_json to reflect the new full step list (done + new).
        t = session.get(Task, task_id)
        if t:
            done_meta = [
                {"index": r.step_index, "description": r.description}
                for r in rows if r.status == "done"
            ]
            new_meta = [
                {"index": max_done + offset, "description": d}
                for offset, (_p, d) in enumerate(new_steps, start=1)
            ]
            t.plan_json = json.dumps(sorted(done_meta + new_meta, key=lambda x: x["index"]))
            t.updated_at = datetime.utcnow()

        session.commit()
        return inserted


# ---------------------------------------------------------------------------
# Legacy (in-memory) path — preserved EXACTLY for task_id=None callers so the
# existing test_orchestrator.py suite (which patches sqlmodel.Session) passes.
# ---------------------------------------------------------------------------

async def _run_task_legacy(task_prompt: str) -> TaskResult:
    from sqlmodel import Session

    from backend.database import AgentRun, engine

    plan = await _opus_plan(task_prompt)
    logger.info(f"Task plan: {len(plan.steps)} steps")

    debug = None
    for _attempt in range(MAX_RETRIES):
        results = []
        failed_step = None

        for step in plan.steps:
            t_start = time.time()
            result = await _sonnet_execute(step, results)
            elapsed = int((time.time() - t_start) * 1000)

            with Session(engine) as session:
                session.add(AgentRun(
                    task_id=None,
                    agent_type="orchestrator",
                    model="sonnet",
                    prompt_snippet=step.prompt[:200],
                    output_snippet=result[:200],
                    success=not _is_failure(result),
                    duration_ms=elapsed,
                ))
                session.commit()

            if _is_failure(result):
                failed_step = (step, result)
                logger.warning(f"Step {step.index} failed: {result[:100]}")
                break

            results.append(result)

        if failed_step is None:
            return TaskResult(success=True, output=results, plan=plan)

        debug = await _opus_debug(task_prompt, plan, failed_step, results)

        if debug.get("action") == "ABORT":
            break
        elif debug.get("action") == "REPLAN" and "new_plan" in debug:
            new_steps = debug["new_plan"].get("steps", [])
            plan.steps = [Step(index=s["index"], prompt=s["prompt"], description=s.get("description", "")) for s in new_steps]
        elif debug.get("action") == "RETRY_STEP" and "new_prompt" in debug:
            plan.patch_step(failed_step[0], debug["new_prompt"])

    reason = "max_retries_exceeded"
    if debug and isinstance(debug, dict) and debug.get("reason"):
        reason = debug["reason"]

    return TaskResult(success=False, reason=reason)


def _plan_to_persist_tuples(plan: Plan) -> list:
    return [(s.index, s.prompt, s.description) for s in plan.steps]


def _steps_to_plan(task_prompt: str, steps: list[dict]) -> Plan:
    plan = Plan(task_prompt=task_prompt)
    for s in steps:
        plan.steps.append(Step(index=s["step_index"], prompt=s["prompt"], description=s["description"]))
    return plan


async def run_task(task_prompt: str, task_id: int | None = None) -> TaskResult:
    # Backward-compat: no task_id -> legacy in-memory loop (existing tests).
    if task_id is None:
        return await _run_task_legacy(task_prompt)

    # --- DURABLE PATH ---------------------------------------------------------
    # 2.1 Entry: load any existing steps. Empty -> first run (plan); non-empty
    # -> RESUME (rebuild plan from rows, do NOT re-plan).
    steps = await asyncio.to_thread(_load_steps, task_id)
    if not steps:
        learning = await asyncio.to_thread(_load_learning_context)
        plan = await _opus_plan(task_prompt, learning=learning)
        logger.info(f"Task {task_id} plan: {len(plan.steps)} steps")
        await asyncio.to_thread(_persist_plan, task_id, _plan_to_persist_tuples(plan))
        steps = await asyncio.to_thread(_load_steps, task_id)
    else:
        logger.info(f"Task {task_id} resuming with {len(steps)} existing step(s)")

    plan = _steps_to_plan(task_prompt, steps)

    # Per-task budget brake reference point. Spend recorded after this instant is
    # attributed to this task. Used by check_budget(task_id, task_start) below.
    from backend.agents.router import (
        TaskAborted,
        reset_task_context,
        set_task_context,
    )
    from backend.safety.governor import BudgetExceeded, check_budget
    task_start = datetime.utcnow()

    # Bind the task_id contextvar so every SpendLog row written by _record_spend
    # (in the run_in_executor worker thread) is tagged with this task. RESET on
    # every exit path via the Token in the finally below. The legacy (task_id is
    # None) path above returns before this and never sets the contextvar.
    _ctx_token = set_task_context(task_id)

    from backend.safety.governor import get_system_state

    debug = None
    try:
        try:
            for _attempt in range(MAX_RETRIES):
                # 2.2 / 2.4: reload steps + rebuild context from DONE outputs each pass.
                steps = await asyncio.to_thread(_load_steps, task_id)
                context = await asyncio.to_thread(_load_done_outputs, task_id)
                failed = None

                for s in steps:
                    if s["status"] == "done":
                        continue  # 2.5 resume — skip already-completed steps

                    # ITEM 3 poison-step ceiling: a step that has already been
                    # attempted MAX_STEP_ATTEMPTS times trips here BEFORE we mark
                    # it running (so an exhausted step trips on resume too).
                    if s["attempts"] >= MAX_STEP_ATTEMPTS:
                        await asyncio.to_thread(
                            _finalize_task,
                            task_id,
                            "failed",
                            json.dumps({
                                "error": "step_exhausted",
                                "step_index": s["step_index"],
                                "attempts": s["attempts"],
                            }),
                        )
                        return TaskResult(success=False, reason="step_exhausted")

                    # Per-step gate order (documented): BUDGET -> KILL -> CANCEL.
                    # 1) Per-task + daily budget brake. A BudgetExceeded (here OR
                    # bubbling from _run mid-call inside _sonnet_execute) is caught
                    # below and finalizes 'failed'/budget_exceeded.
                    await asyncio.to_thread(check_budget, task_id, task_start)

                    # 2) Kill switch compute-gate: if autonomy was disabled mid-task
                    # (e.g. POST /api/safety/pause), stop before the next step's LLM
                    # call. Finalize 'stopped' with autonomy_disabled; done preserved.
                    state = await asyncio.to_thread(get_system_state)
                    if not state["autonomy_enabled"]:
                        await asyncio.to_thread(
                            _finalize_task,
                            task_id,
                            "stopped",
                            json.dumps({"error": "autonomy_disabled"}),
                        )
                        return TaskResult(success=False, reason="stopped")

                    # 3) Cooperative cancel checked before marking running.
                    if await asyncio.to_thread(_is_cancel_requested, task_id):
                        await asyncio.to_thread(_finalize_task, task_id, "stopped", None)
                        return TaskResult(success=False, reason="cancelled")

                    await asyncio.to_thread(_mark_step_running, s["id"])

                    step_obj = Step(index=s["step_index"], prompt=s["prompt"], description=s["description"])
                    t_start = time.time()
                    # Pass task context into the tool-use loop so it can enforce
                    # kill/budget/cancel BETWEEN tool rounds (not just between steps).
                    result = await _sonnet_execute(step_obj, context, task_id=task_id, task_start=task_start)
                    elapsed = int((time.time() - t_start) * 1000)

                    success = not _is_failure(result)
                    await asyncio.to_thread(
                        _record_agent_run, task_id, step_obj.prompt, result, success, elapsed
                    )

                    if not success:
                        await asyncio.to_thread(_fail_step, s["id"], result)
                        failed = (step_obj, result)
                        logger.warning(f"Task {task_id} step {step_obj.index} failed: {result[:100]}")
                        break

                    await asyncio.to_thread(_complete_step, s["id"], result)  # checkpoint
                    context.append(result)

                if failed is None:
                    outputs = await asyncio.to_thread(_load_done_outputs, task_id)
                    # Opus verifier: honest success gate. This runs INSIDE the
                    # try/except so BudgetExceeded / TaskAborted from the verify
                    # call propagate to the existing handlers (finalize
                    # failed/budget_exceeded or stopped). The verify prompt is
                    # built from final outputs; a parse failure returns the safe
                    # permissive default (verdict="uncertain") so a verifier crash
                    # NEVER destroys a genuinely-good completed task.
                    outcome = await _opus_verify(
                        task_prompt, outputs, plan,
                        task_id=task_id, task_start=task_start,
                    )
                    await asyncio.to_thread(
                        _record_task_outcome,
                        task_id,
                        outcome["verdict"],
                        outcome["confidence"],
                        outcome["reason"],
                        outcome["grounded"],
                        outcome.get("evidence"),
                    )
                    # GATING RULE: only a confident rejection overturns a
                    # completed task. verdict=="failure" AND confidence>=0.7
                    # finalizes "failed"; everything else (success/partial/
                    # uncertain/low-confidence failure) finalizes "success".
                    if outcome["verdict"] == "failure" and outcome["confidence"] >= 0.7:
                        await asyncio.to_thread(
                            _finalize_task,
                            task_id,
                            "failed",
                            json.dumps({
                                "error": "verify_rejected",
                                "reason": outcome["reason"],
                                "confidence": outcome["confidence"],
                            }),
                        )
                        return TaskResult(success=False, reason="verify_rejected")

                    await asyncio.to_thread(
                        _finalize_task, task_id, "success", json.dumps(outputs)
                    )
                    return TaskResult(success=True, output=outputs, plan=plan)

                # 2.2 debug the failure.
                prior = await asyncio.to_thread(_load_done_outputs, task_id)
                debug = await _opus_debug(task_prompt, plan, failed, prior)
                action = debug.get("action") if isinstance(debug, dict) else None

                if action == "ABORT":
                    break
                elif action == "REPLAN" and "new_plan" in debug:
                    raw_steps = debug["new_plan"].get("steps", [])
                    new_steps = [
                        (s["prompt"], s.get("description", "")) for s in raw_steps
                    ]
                    if not new_steps:
                        # 2.6 empty replan -> ABORT.
                        debug = {"action": "ABORT", "reason": "replan_empty"}
                        break
                    await asyncio.to_thread(_replan_durably, task_id, new_steps)
                    steps = await asyncio.to_thread(_load_steps, task_id)
                    plan = _steps_to_plan(task_prompt, steps)
                elif action == "RETRY_STEP" and "new_prompt" in debug:
                    await asyncio.to_thread(
                        _patch_step_durably, task_id, failed[0].index, debug["new_prompt"]
                    )
        except BudgetExceeded as e:
            # Budget tripped here (the brake) OR mid-call inside _sonnet_execute (the
            # router's daily brake). Finalize 'failed' with the budget detail. Done
            # steps are preserved on disk (durable) — only the Task row is marked.
            await asyncio.to_thread(
                _finalize_task,
                task_id,
                "failed",
                json.dumps({
                    "error": "budget_exceeded",
                    "scope": e.scope,
                    "spend": e.spend,
                    "cap": e.cap,
                }),
            )
            return TaskResult(success=False, reason="budget_exceeded")
        except TaskAborted as exc:
            # Kill switch (autonomy disabled) or cancel raised INSIDE the tool-use
            # loop, between tool rounds. Finalize 'stopped'; done steps preserved.
            await asyncio.to_thread(
                _finalize_task,
                task_id,
                "stopped",
                json.dumps({"error": exc.reason}),
            )
            return TaskResult(success=False, reason=exc.reason)

        reason = "max_retries_exceeded"
        if debug and isinstance(debug, dict) and debug.get("reason"):
            reason = debug["reason"]

        await asyncio.to_thread(
            _finalize_task, task_id, "failed", json.dumps({"error": reason})
        )
        return TaskResult(success=False, reason=reason)
    finally:
        # Unbind the task-id contextvar on EVERY exit path (return / exception).
        reset_task_context(_ctx_token)
