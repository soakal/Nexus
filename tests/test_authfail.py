from backend.safety import authfail


def setup_function():
    authfail.reset()


def test_record_and_recent_counts():
    authfail.record_failure("1.2.3.4", "/api/health", now=100.0)
    authfail.record_failure("1.2.3.4", "/api/health", now=101.0)
    authfail.record_failure("1.2.3.4", "/api/health", now=102.0)
    stats = authfail.recent(600, now=102.0)
    assert stats["1.2.3.4"]["count"] == 3


def test_window_prunes_old_events():
    authfail.record_failure("1.2.3.4", "/api/health", now=1000.0)
    stats = authfail.recent(600, now=1000.0 + 601)
    assert "1.2.3.4" not in stats


def test_paths_top_three_descending():
    now = 100.0
    for path, n in [("/a", 5), ("/b", 3), ("/c", 2), ("/d", 1)]:
        for _ in range(n):
            authfail.record_failure("1.2.3.4", path, now=now)
    stats = authfail.recent(600, now=now)
    paths = stats["1.2.3.4"]["paths"]
    assert len(paths) == 3
    assert [p for p, _ in paths] == ["/a", "/b", "/c"]
    assert paths[0][1] >= paths[1][1] >= paths[2][1]


def test_sources_are_isolated():
    authfail.record_failure("1.1.1.1", "/x", now=100.0)
    authfail.record_failure("1.1.1.1", "/x", now=100.0)
    authfail.record_failure("2.2.2.2", "/y", now=100.0)
    stats = authfail.recent(600, now=100.0)
    assert stats["1.1.1.1"]["count"] == 2
    assert stats["2.2.2.2"]["count"] == 1


def test_max_sources_evicts_oldest():
    for i in range(authfail.MAX_SOURCES + 1):
        authfail.record_failure(f"src-{i}", "/x", now=float(i))
    stats = authfail.recent(10_000, now=float(authfail.MAX_SOURCES))
    assert len(stats) <= authfail.MAX_SOURCES
    assert "src-0" not in stats


def test_per_source_history_is_bounded():
    for i in range(authfail.MAX_EVENTS_PER_SOURCE + 100):
        authfail.record_failure("1.2.3.4", "/x", now=float(i))
    stats = authfail.recent(10_000, now=float(authfail.MAX_EVENTS_PER_SOURCE + 100))
    assert stats["1.2.3.4"]["count"] == authfail.MAX_EVENTS_PER_SOURCE
    assert stats["1.2.3.4"]["count"] > 25  # saturation must never suppress the default threshold


def test_record_failure_never_raises():
    authfail.record_failure(None, None)
    authfail.record_failure("", "")
    authfail.record_failure(12345, object())  # type: ignore[arg-type]
