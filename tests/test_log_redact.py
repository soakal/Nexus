import io
import logging

import httpx

from backend.log_redact import install, redact


def test_redact_masks_url_borne_secrets():
    samples = {
        "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates": "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        "https://api.openweathermap.org/data/2.5/weather?lat=42.35&appid=0123456789abcdef0123456789abcdef": "0123456789abcdef0123456789abcdef",
        "https://calendar.google.com/calendar/ical/x%40group.calendar.google.com/private-0123456789abcdef0123456789abcdef/basic.ics": "0123456789abcdef0123456789abcdef",
        "https://p42-caldav.icloud.com/published/2/MTIzNDU2Nzg5MDEyMzQ1Njc4OTBhYmNk_efgh": "MTIzNDU2Nzg5MDEyMzQ1Njc4OTBhYmNk_efgh",
    }
    for url, secret in samples.items():
        out = redact(url)
        assert secret not in out, out
        assert "<redacted>" in out


def test_installed_formatter_redacts_logged_exception_and_traceback():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log = logging.getLogger("redact-test")
    log.addHandler(handler)
    log.propagate = False
    install("redact-test")
    url = "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates"
    req = httpx.Request("GET", url)
    err = httpx.HTTPStatusError("Server error '502 Bad Gateway' for url '%s'" % url, request=req, response=httpx.Response(502, request=req))
    try:
        raise err
    except httpx.HTTPStatusError as e:
        log.warning(f"poller error: {e!r}")
        log.exception("boom")
    out = stream.getvalue()
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    assert out.count("bot<redacted>") >= 2
    assert out.startswith("WARNING poller error")
