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
