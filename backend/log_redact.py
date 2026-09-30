"""Mask credentials that ride inside request URLs before they reach the journal.

httpx's HTTPStatusError / exception text includes the full URL, and several
integrations carry their secret IN the URL: the Telegram bot token, the
OpenWeather appid, the private Google/iCloud calendar feed tokens. Muting the
httpx logger (main.py) doesn't cover a caller logging the exception itself, so
redaction happens at the formatter, after the traceback is rendered.
"""
import logging
import re

_PATTERNS = [
    (re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"), r"\1<redacted>"),
    (re.compile(r"((?:appid|api_?key|access_token|token|key)=)[^&\s'\"]+", re.I), r"\1<redacted>"),
    (re.compile(r"(/private-)[0-9a-f]{16,}"), r"\1<redacted>"),
    (re.compile(r"(/published/\d+/)[A-Za-z0-9_-]{20,}"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class _RedactingFormatter(logging.Formatter):
    def __init__(self, inner: logging.Formatter | None):
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))


def install(*logger_names: str) -> None:
    """Wrap the formatter of every handler on the root logger and the named ones."""
    for name in ("", *logger_names):
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(handler.formatter))
