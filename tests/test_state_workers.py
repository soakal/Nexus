def test_every_source_has_a_state_collector():
    # Imports the REAL registries (not a hardcoded copy) so this test actually
    # fails if a 14th integration is added to sources.py without a matching
    # state_workers.py collector — a copied literal would drift silently
    # instead (same pattern as test_contract_canary.py's coverage check).
    from backend.api.sources import REGISTRY_NAMES
    from backend.state_workers import COLLECTOR_GROUPS

    covered = {
        key[len("source."):]
        for group in COLLECTOR_GROUPS.values()
        for key in (c.key for c in group)
        if key.startswith("source.")
    }
    missing = set(REGISTRY_NAMES) - covered
    assert not missing, (
        f"Integration(s) {missing} are in backend/api/sources.py's REGISTRY_NAMES but have "
        f"no source.* collector registered in backend/state_workers.py's COLLECTOR_GROUPS — "
        f"the dashboard's Sources card would silently show never_observed for it forever."
    )


def test_claude_usage_is_dashboard_only_not_a_registered_source():
    # Real, imported registries -- not a hardcoded copy -- so this can't be
    # silently "corrected" later. claude_usage's file is legitimately hours
    # old whenever no Claude Code session is running (normal operation, not
    # an outage), so there is no honest up/down health signal -- registering
    # it as a source would pin a permanently-OFFLINE tile on the Sources card
    # for a feature that's working correctly.
    from backend.api.sources import REGISTRY_NAMES
    from backend.state_workers import COLLECTOR_GROUPS

    all_keys = {c.key for group in COLLECTOR_GROUPS.values() for c in group}
    assert "dashboard.claude_usage" in all_keys
    assert "source.claude_usage" not in all_keys
    assert "claude_usage" not in REGISTRY_NAMES


def test_openrouter_has_both_a_source_and_dashboard_collector():
    # Unlike claude_usage, openrouter's data genuinely is a live network call
    # with a meaningful up/down signal -- it keeps its pre-existing
    # source.openrouter health-check collector AND gains a dashboard.*
    # collector (2026-08-05) now that a real consumer (Dashboard.jsx's
    # OpenRouter card, briefing.py's narrated section) reads its credit data.
    from backend.state_workers import COLLECTOR_GROUPS

    all_keys = {c.key for group in COLLECTOR_GROUPS.values() for c in group}
    assert "source.openrouter" in all_keys
    assert "dashboard.openrouter" in all_keys


def test_no_duplicate_collector_keys():
    # Two collectors racing to write the same StateSnapshot key was a real bug
    # found during this feature's live smoke test (see state_store.py's
    # _get_or_create) -- a duplicate key registered by mistake would
    # reintroduce that exact race on every refresh, not just at cold boot.
    from backend.state_workers import COLLECTOR_GROUPS

    seen = set()
    dupes = set()
    for group in COLLECTOR_GROUPS.values():
        for collector in group:
            if collector.key in seen:
                dupes.add(collector.key)
            seen.add(collector.key)
    assert not dupes, f"Duplicate collector key(s) registered across COLLECTOR_GROUPS: {dupes}"


def test_state_ws_manager_is_distinct_from_logs_ws_manager():
    # A real bug found by review: /ws/state sharing /ws/logs's broadcaster
    # would leak state.updated JSON into the Safety/Traces log viewer
    # (AgentLog.jsx renders every incoming message with zero type filtering).
    # This must stay two separate WebSocketManager instances, not two names
    # for the same one.
    from backend.api.agents import state_ws_manager, ws_manager

    assert state_ws_manager is not ws_manager
    assert state_ws_manager.active is not ws_manager.active


def test_prime_state_workers_task_reference_is_captured():
    # Regression guard for a real bug found by review: asyncio.create_task()
    # only holds a weak reference internally, so a bare
    # `asyncio.create_task(prime_state_workers())` statement (return value
    # discarded) leaves the multi-collector boot prime eligible for silent
    # GC mid-run. This is a static-source check, not a live-timing test
    # (same "paren-scanning regression guard" style as
    # test_spend_report.py::test_no_unlabeled_llm_calls_in_agents) -- it
    # fails if the call site ever regresses back to a bare, unassigned call.
    import inspect
    import re

    from backend import main

    lines = [l for l in inspect.getsource(main).splitlines() if "prime_state_workers())" in l]
    assert lines, "prime_state_workers() call not found in backend/main.py"

    assert re.match(r"\s*\w+\s*=\s*asyncio\.create_task\(prime_state_workers\(\)\)", lines[0]), (
        "prime_state_workers()'s task must be assigned to a variable (kept as a strong "
        "reference for the lifespan generator's suspended frame to hold), not called as a "
        "bare, discarded expression statement -- see the comment at this call site in main.py."
    )
