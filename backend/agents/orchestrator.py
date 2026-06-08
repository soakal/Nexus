import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


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


async def run_task(task_prompt: str, task_id: int | None = None) -> TaskResult:
    from sqlmodel import Session

    from backend.database import AgentRun, Task, engine

    plan = await _opus_plan(task_prompt)
    logger.info(f"Task plan: {len(plan.steps)} steps")

    if task_id:
        with Session(engine) as session:
            t = session.get(Task, task_id)
            if t:
                t.plan_json = json.dumps([{"index": s.index, "description": s.description} for s in plan.steps])
                t.status = "running"
                session.commit()

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
                    task_id=task_id,
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
            if task_id:
                with Session(engine) as session:
                    t = session.get(Task, task_id)
                    if t:
                        t.status = "success"
                        t.result_json = json.dumps(results)
                        t.steps_taken = len(results)
                        session.commit()
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

    if task_id:
        with Session(engine) as session:
            t = session.get(Task, task_id)
            if t:
                t.status = "failed"
                t.result_json = json.dumps({"error": reason})
                session.commit()

    return TaskResult(success=False, reason=reason)
