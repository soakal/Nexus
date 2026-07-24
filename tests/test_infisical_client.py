import httpx
import pytest

from backend.secrets import infisical_client as ic

# Captured at collection time, before conftest's autouse mock_secrets fixture
# monkeypatches infisical_client._request into a hard network guard for the
# rest of the suite — these tests exercise the real request path against a
# MockTransport, so they restore it explicitly (innermost patch wins).
_real_request = ic._request


class _StubSettings:
    infisical_url = "https://infisical.test"
    infisical_client_id = "test-client-id"
    infisical_client_secret = "test-client-secret"
    infisical_project_id = "test-project-id"
    infisical_env = "prod"
    infisical_cache_ttl_s = 300


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """infisical_client keeps module-level cache/token state — reset it between
    tests so one test's cache/token can't leak into another."""
    monkeypatch.setattr(ic, "_cache", {})
    monkeypatch.setattr(ic, "_meta", {})
    monkeypatch.setattr(ic, "_known_folders", set())
    monkeypatch.setattr(ic, "_token", None)
    monkeypatch.setattr(ic, "_last_fetch_ok", False)
    monkeypatch.setattr(ic, "_last_fetch_failed_at", None)
    monkeypatch.setattr(ic, "_refresh_thread_started", True)  # never spawn a real thread in tests
    monkeypatch.setattr(ic, "_settings", lambda: _StubSettings())
    monkeypatch.setattr(ic, "_request", _real_request)


def _patch_transport(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


def _login_response(request):
    return httpx.Response(200, json={"accessToken": "test-token"})


def test_secret_path_to_vault_key_flat():
    assert ic._secret_path_to_vault_key("/", "ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY"


def test_secret_path_to_vault_key_cred_folder():
    assert ic._secret_path_to_vault_key("/creds/hermes", "PASSWORD") == "cred:hermes:password"


def test_secret_path_to_vault_key_unknown_nesting_ignored():
    assert ic._secret_path_to_vault_key("/some/other/deep/path", "X") is None


def test_vault_key_to_path_and_name_flat():
    assert ic._vault_key_to_path_and_name("NEXUS_API_KEY") == ("/", "NEXUS_API_KEY")


def test_vault_key_to_path_and_name_cred_round_trip():
    path, name = ic._vault_key_to_path_and_name("cred:hermes:password")
    assert path == "/creds/hermes"
    assert name == "PASSWORD"
    assert ic._secret_path_to_vault_key(path, name) == "cred:hermes:password"


def test_vault_key_to_path_and_name_rejects_invalid_service_name():
    with pytest.raises(ValueError):
        ic._vault_key_to_path_and_name("cred:HERMES!:password")


def test_bulk_fetch_populates_cache_including_cred_keys(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        return httpx.Response(200, json={"secrets": [
            {"secretKey": "ANTHROPIC_API_KEY", "secretPath": "/", "secretValue": "sk-ant-x", "updatedAt": "2026-01-01T00:00:00Z"},
            {"secretKey": "PASSWORD", "secretPath": "/creds/hermes", "secretValue": "hunter2", "updatedAt": "2026-01-01T00:00:00Z"},
        ]})

    _patch_transport(monkeypatch, handler)
    assert ic._bulk_fetch() is True
    assert ic.get_secret("ANTHROPIC_API_KEY") == "sk-ant-x"
    assert ic.get_secret("cred:hermes:password") == "hunter2"
    assert set(ic.list_keys()) == {"ANTHROPIC_API_KEY", "cred:hermes:password"}


def test_get_secret_cold_cache_triggers_synchronous_fetch(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        calls["n"] += 1
        return httpx.Response(200, json={"secrets": [
            {"secretKey": "NEXUS_API_KEY", "secretPath": "/", "secretValue": "abc123", "updatedAt": None},
        ]})

    _patch_transport(monkeypatch, handler)
    assert ic.get_secret("NEXUS_API_KEY") == "abc123"
    assert calls["n"] == 1


def test_get_secret_missing_key_raises_key_error(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        return httpx.Response(200, json={"secrets": []})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(KeyError):
        ic.get_secret("NOPE")


def test_get_secret_raises_runtime_error_when_unreachable_and_no_cache(monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        ic.get_secret("ANTHROPIC_API_KEY")


def test_stale_cache_served_on_refresh_failure(monkeypatch):
    monkeypatch.setattr(ic, "_cache", {"ANTHROPIC_API_KEY": "sk-ant-stale"})
    monkeypatch.setattr(ic, "_last_fetch_ok", True)

    def handler(request):
        return httpx.Response(503, json={"error": "down"})

    _patch_transport(monkeypatch, handler)
    ok = ic._bulk_fetch()
    assert ok is False
    # Existing cache must be untouched by a failed refresh.
    assert ic.get_secret("ANTHROPIC_API_KEY") == "sk-ant-stale"


def test_401_triggers_single_relogin_retry(monkeypatch):
    monkeypatch.setattr(ic, "_token", "expired-token")
    call_log = []

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            call_log.append("login")
            return _login_response(request)
        call_log.append("data")
        if call_log.count("data") == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"secrets": []})

    _patch_transport(monkeypatch, handler)
    resp = ic._request("GET", "/api/v3/secrets/raw", params={})
    assert resp.status_code == 200
    assert call_log == ["data", "login", "data"]


def test_list_credentials_never_includes_password_value(monkeypatch):
    monkeypatch.setattr(ic, "_cache", {
        "cred:hermes:host": "1.2.3.4",
        "cred:hermes:user": "root",
        "cred:hermes:password": "hunter2",
    })
    creds = ic.list_credentials()
    assert creds["hermes"]["has_password"] is True
    assert creds["hermes"]["host"] == "1.2.3.4"
    assert "hunter2" not in str(creds)


def test_set_secret_creates_folder_before_writing_cred(monkeypatch):
    calls = []

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/folders":
            return httpx.Response(200, json={"folder": {"path": "/creds/newsvc"}})
        return httpx.Response(200, json={"secret": {}})

    _patch_transport(monkeypatch, handler)
    ic.set_secret("cred:newsvc:host", "10.0.0.1")
    assert ("POST", "/api/v1/folders") in calls
    assert ic.get_secret("cred:newsvc:host") == "10.0.0.1"


def test_set_secret_folder_already_exists_400_is_treated_as_success(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        if request.url.path == "/api/v1/folders":
            return httpx.Response(400, json={"message": "Folder with name 'x' already exists"})
        return httpx.Response(200, json={"secret": {}})

    _patch_transport(monkeypatch, handler)
    ic.set_secret("cred:existingsvc:host", "10.0.0.2")  # must not raise


def test_delete_secret_missing_key_is_a_noop(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        return httpx.Response(404, json={"error": "not found"})

    _patch_transport(monkeypatch, handler)
    ic.delete_secret("SOME_KEY_NOT_PRESENT")  # must not raise


def test_module_never_writes_to_disk():
    """No plaintext-at-rest guarantee successor to test_vault_is_encrypted —
    under Infisical the guarantee is 'no plaintext on this disk at all'."""
    import inspect
    src = inspect.getsource(ic)
    for banned in ("open(", "Path(", "pathlib", ".write_text", ".write_bytes"):
        assert banned not in src, f"infisical_client.py must never touch the filesystem (found {banned!r})"


def test_cooldown_suppresses_reattempt_storm(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        calls["n"] += 1
        return httpx.Response(500, json={"error": "down"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        ic.get_secret("ANY")
    first_count = calls["n"]
    assert first_count > 0

    with pytest.raises(RuntimeError):
        ic.get_secret("ANY")
    assert calls["n"] == first_count  # no additional HTTP requests at all


def test_cooldown_expiry_reenables_synchronous_retry(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(ic, "_monotonic", lambda: clock["now"])
    calls = {"n": 0}
    state = {"up": False}

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        calls["n"] += 1
        if not state["up"]:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={"secrets": [
            {"secretKey": "NEXUS_API_KEY", "secretPath": "/", "secretValue": "abc123", "updatedAt": None},
        ]})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        ic.get_secret("NEXUS_API_KEY")
    count_after_failure = calls["n"]

    # Still within the cooldown — no new attempt.
    with pytest.raises(RuntimeError):
        ic.get_secret("NEXUS_API_KEY")
    assert calls["n"] == count_after_failure

    # Advance past the cooldown and let the backend recover.
    clock["now"] += ic.FETCH_FAILURE_COOLDOWN_S + 1
    state["up"] = True
    assert ic.get_secret("NEXUS_API_KEY") == "abc123"
    assert calls["n"] > count_after_failure


def test_cooldown_does_not_block_bulk_fetch_or_warm_up(monkeypatch):
    calls = {"n": 0}
    state = {"up": False}

    def handler(request):
        if request.url.path.endswith("/auth/universal-auth/login"):
            return _login_response(request)
        calls["n"] += 1
        if not state["up"]:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={"secrets": [
            {"secretKey": "NEXUS_API_KEY", "secretPath": "/", "secretValue": "abc123", "updatedAt": None},
        ]})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        ic.get_secret("NEXUS_API_KEY")
    count_after_failure = calls["n"]

    # Cooldown is active, but a direct _bulk_fetch() (the warm_up/refresh-loop
    # path) must still make a real attempt, unblocked by the cooldown.
    state["up"] = True
    assert ic._bulk_fetch() is True
    assert calls["n"] > count_after_failure
    assert ic.get_secret("NEXUS_API_KEY") == "abc123"
