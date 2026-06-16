import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


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


async def _opus_plan(task_prompt: str) -> Plan:
    from backend.agents.router import opus
    plan_prompt = f"""Decompose this task into numbered execution steps for an executor agent.

TASK: {task_prompt}

THE EXECUTOR HAS THESE TOOLS:
- WEB_SEARCH: <query>  — live web search (DuckDuckGo + GitHub releases API).
  Use for current versions, news, prices, dates, or any real-time information.
- VAULT_SEARCH: <query> — searches the user's personal Obsidian knowledge vault.
  Use when the task mentions "my notes", "my vault", "Obsidian", "vault", or asks
  to find/retrieve/summarize something from personal notes or saved knowledge.

MANDATORY RULES — follow these exactly:
1. If ANY step needs current/live/real-time information, that step's "prompt" MUST
   literally begin with "WEB_SEARCH:" followed by a self-contained search query.
   CORRECT: "WEB_SEARCH: HashiCorp Vault latest stable release version"
   WRONG:   "Retrieve live data from authoritative sources to find the version"
2. If ANY step needs to look up the user's personal notes or Obsidian vault, that
   step's "prompt" MUST literally begin with "VAULT_SEARCH:" followed by the query.
   CORRECT: "VAULT_SEARCH: project ideas"
   CORRECT: "VAULT_SEARCH: meeting notes April"
   WRONG:   "Search the vault for project ideas"
3. NEVER put placeholders inside a tool query. Queries run verbatim — no substitution.
4. For tasks answerable from general knowledge alone, use no tool prefix.
5. Keep it minimal: usually 1-3 steps. The final step synthesizes the answer.

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


async def _sonnet_execute(step: Step, context: list) -> str:
    import re

    from backend.agents.router import sonnet
    from backend.integrations.web_search import search

    # Intercept VAULT_SEARCH directives — search the user's Obsidian vault
    vault_marker = re.search(r"VAULT_SEARCH:\s*(.+?)(?:\n|$)", step.prompt, re.IGNORECASE)
    if vault_marker:
        from backend.integrations.obsidian import vault_search
        query = re.sub(r"<[^>]*>", "", vault_marker.group(1).strip()).strip()
        vault_result = await vault_search(query)
        enriched_prompt = step.prompt + f"\n\nVAULT_SEARCH results for '{query}':\n{vault_result}\n\nSummarize the above vault search results to answer the task."
        context_str = "\n".join([f"Step {i+1} result: {r}" for i, r in enumerate(context)]) if context else "No prior context."
        full_prompt = f"""Previous results:
{context_str}

Current task:
{enriched_prompt}

Execute this task and return the result directly."""
        return await sonnet(full_prompt)

    # Intercept WEB_SEARCH directives before sending to LLM
    web_marker = re.search(r"WEB_SEARCH:\s*(.+?)(?:\n|$)", step.prompt, re.IGNORECASE)
    if web_marker:
        query = web_marker.group(1).strip()
        # Strip any leftover "<placeholder>" tokens the planner may have emitted —
        # the executor cannot substitute prior-step values into a literal query.
        query = re.sub(r"<[^>]*>", "", query).replace("  ", " ").strip()
        web_result = await search(query)
        # Append search results to context and continue with enriched prompt
        enriched_prompt = step.prompt + f"\n\nWEB_SEARCH results for '{query}':\n{web_result}\n\nSummarize the above search results to answer the task."
        context_str = "\n".join([f"Step {i+1} result: {r}" for i, r in enumerate(context)]) if context else "No prior context."
        full_prompt = f"""Previous results:
{context_str}

Current task:
{enriched_prompt}

Execute this task and return the result directly."""
        return await sonnet(full_prompt)

    context_str = "\n".join([f"Step {i+1} result: {r}" for i, r in enumerate(context)]) if context else "No prior context."
    full_prompt = f"""Previous results:
{context_str}

Current task:
{step.prompt}

Execute this task and return the result directly.

IMPORTANT: You do NOT have live internet access in this step. If the task asks
for current/real-time information and no web search results were provided above,
DO NOT refuse. Instead, give your best answer from your training knowledge and
clearly state the knowledge cutoff caveat, e.g. "Based on my training knowledge
(as of my cutoff), the answer is X — verify against the live source for the most
current value." Always produce a useful, substantive answer."""
    return await sonnet(full_prompt)


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
        plan = await _opus_plan(task_prompt)
        logger.info(f"Task {task_id} plan: {len(plan.steps)} steps")
        await asyncio.to_thread(_persist_plan, task_id, _plan_to_persist_tuples(plan))
        steps = await asyncio.to_thread(_load_steps, task_id)
    else:
        logger.info(f"Task {task_id} resuming with {len(steps)} existing step(s)")

    plan = _steps_to_plan(task_prompt, steps)

    debug = None
    for _attempt in range(MAX_RETRIES):
        # 2.2 / 2.4: reload steps + rebuild context from DONE outputs each pass.
        steps = await asyncio.to_thread(_load_steps, task_id)
        context = await asyncio.to_thread(_load_done_outputs, task_id)
        failed = None

        for s in steps:
            if s["status"] == "done":
                continue  # 2.5 resume — skip already-completed steps

            # 2.3 cooperative cancel checked before marking running.
            if await asyncio.to_thread(_is_cancel_requested, task_id):
                await asyncio.to_thread(_finalize_task, task_id, "stopped", None)
                return TaskResult(success=False, reason="cancelled")

            await asyncio.to_thread(_mark_step_running, s["id"])

            step_obj = Step(index=s["step_index"], prompt=s["prompt"], description=s["description"])
            t_start = time.time()
            result = await _sonnet_execute(step_obj, context)
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

    reason = "max_retries_exceeded"
    if debug and isinstance(debug, dict) and debug.get("reason"):
        reason = debug["reason"]

    await asyncio.to_thread(
        _finalize_task, task_id, "failed", json.dumps({"error": reason})
    )
    return TaskResult(success=False, reason=reason)
