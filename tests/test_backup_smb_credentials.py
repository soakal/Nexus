"""Tests for backend/backup.py::_mount_unc's credential-passing mechanism.

F11: the SMB backup password must never appear as a literal argv element to
a child process (visible to any co-resident process/user via process
listing for the child's whole lifetime). _mount_unc now shells out to
PowerShell's New-SmbMapping with the ENTIRE script -- including
base64-encoded credentials -- piped over the child's STDIN, never argv.

These tests verify, structurally and (where practical) empirically against
a real PowerShell child process:
  1. the password is never present in the subprocess argv list
  2. the piped script is a single line (no embedded \\n / \\r)
  3. a real wrong-password/wrong-share failure's captured stderr/log output
     never contains the plaintext password
  4. a non-ASCII credential survives the base64 round-trip through a REAL
     PowerShell 5.1 process reading it off stdin (not just on paper --
     Windows PowerShell 5.1 decodes redirected stdin using the console's
     OEM code page, which can silently corrupt non-ASCII text embedded
     directly in a piped script)
"""
import base64
import logging
import re
import shutil
import subprocess
import pytest
from unittest.mock import MagicMock

from backend.backup import _mount_unc

_HAS_POWERSHELL = shutil.which("powershell") is not None


def _fake_settings(user="bob", password="S3cretPassw0rd!42"):
    s = MagicMock()
    s.unraid_backup_user = user
    s.unraid_backup_password = password
    return s


def _capture_run(monkeypatch):
    """Patch subprocess.run to record the exact argv + stdin without
    actually spawning PowerShell, and return the dict it fills in."""
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        result = MagicMock()
        result.returncode = 0
        result.stdout = b""
        result.stderr = b""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


# ---------------------------------------------------------------------------
# 1. Password never appears as a literal argv element
# ---------------------------------------------------------------------------

def test_password_never_in_argv(monkeypatch):
    captured = _capture_run(monkeypatch)
    settings = _fake_settings(password="S3cretPassw0rd!42")

    _mount_unc(r"\\192.168.1.50\Computer Backup\Nexus_backup", settings)

    assert "cmd" in captured, "subprocess.run (PowerShell) was never invoked"
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    for arg in cmd:
        assert "S3cretPassw0rd!42" not in str(arg), (
            f"plaintext password found in argv element: {arg!r}"
        )
    # The whole argv, joined, must not contain the password either.
    assert "S3cretPassw0rd!42" not in " ".join(str(a) for a in cmd)


def test_password_passed_via_stdin_base64_not_argv(monkeypatch):
    """Structural check: the credential travels in `input=` (stdin), base64
    encoded, never in argv -- and the raw plaintext isn't even embedded in
    the piped script text (only its base64 form is)."""
    captured = _capture_run(monkeypatch)
    settings = _fake_settings(password="S3cretPassw0rd!42")

    _mount_unc(r"\\192.168.1.50\Computer Backup\Nexus_backup", settings)

    assert captured["input"] is not None, "credentials must be piped via stdin"
    assert isinstance(captured["input"], bytes)
    assert b"S3cretPassw0rd!42" not in captured["input"], (
        "raw plaintext password must not appear verbatim in the piped script"
    )
    pw_b64 = base64.b64encode(b"S3cretPassw0rd!42")
    assert pw_b64 in captured["input"], (
        "base64-encoded password should be present in the piped stdin script"
    )
    # cmd itself uses "-Command -" (read script from stdin), never embeds it.
    cmd_str = " ".join(str(a) for a in captured["cmd"])
    assert "-Command" in cmd_str and cmd_str.strip().endswith("-")


# ---------------------------------------------------------------------------
# 2. The piped script is a single line -- no embedded newlines
# ---------------------------------------------------------------------------

def test_script_is_single_line_pure_ascii(monkeypatch):
    captured = _capture_run(monkeypatch)
    settings = _fake_settings()

    _mount_unc(r"\\192.168.1.50\Computer Backup\Nexus_backup", settings)

    script = captured["input"]
    assert b"\n" not in script, "piped script must not contain a literal newline"
    assert b"\r" not in script, "piped script must not contain a literal carriage return"
    # Pure ASCII -- required so Windows PowerShell 5.1's OEM-codepage stdin
    # decoding can never corrupt it.
    script.decode("ascii")  # raises UnicodeDecodeError if not pure ASCII


def test_persistent_false_flag_present(monkeypatch):
    """-Persistent $false matches the old /persistent:no -- no lingering
    stored credential after the mount attempt."""
    captured = _capture_run(monkeypatch)
    settings = _fake_settings()

    _mount_unc(r"\\192.168.1.50\Computer Backup\Nexus_backup", settings)

    script = captured["input"].decode("ascii")
    assert "-Persistent $false" in script
    assert "Write-Error" not in script, (
        "catch block must not use Write-Error -- it echoes the invoking "
        "script line (including credentials) into stderr"
    )


# ---------------------------------------------------------------------------
# 3. A real failure's captured stderr/log output contains no password
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_POWERSHELL, reason="PowerShell not available on this host")
def test_wrong_password_real_failure_leaks_no_password(caplog):
    """Point at a share that cannot possibly exist so New-SmbMapping fails
    for real, then verify the plaintext password shows up nowhere in the
    logged stdout/stderr."""
    secret_password = "TotallyWrongPassw0rd!Zz9"
    settings = _fake_settings(user="nosuchuser12345", password=secret_password)

    caplog.set_level(logging.DEBUG, logger="backend.backup")
    _mount_unc(r"\\127.0.0.1\NoSuchShareXYZ123", settings)

    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_password not in combined, (
        f"plaintext password leaked into log output: {combined!r}"
    )
    # Sanity: we actually got a debug line about the failed attempt, i.e.
    # this test exercised the failure path it claims to.
    assert any("SMB mount" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Non-ASCII credential round-trips correctly through a REAL PowerShell
#    process reading the base64-decode idiom off stdin.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_POWERSHELL, reason="PowerShell not available on this host")
def test_non_ascii_credential_roundtrips_through_real_powershell(monkeypatch):
    non_ascii_user = "bri\u00e4n-\u65e5\u672c"       # brian with an umlaut + kanji
    non_ascii_pw = "p\u00e4ssw\u00f6rd\u00a3\u20ac\u65e5\u672c\u8a9e!"  # euro/pound/CJK

    # Keep a handle to the REAL subprocess.run before _capture_run patches
    # the module-level attribute -- monkeypatch only reverts it once this
    # test function returns, and we need the real implementation below to
    # actually invoke PowerShell for the round-trip proof.
    real_run = subprocess.run

    captured = _capture_run(monkeypatch)
    settings = _fake_settings(user=non_ascii_user, password=non_ascii_pw)
    _mount_unc(r"\\192.168.1.50\Computer Backup\Nexus_backup", settings)

    script_text = captured["input"].decode("ascii")
    # Pull the exact base64 literals _mount_unc embedded (in emission order:
    # share, password, user) out of the real generated script -- rather than
    # naively splitting the whole script on ";" (the catch block itself
    # contains an internal ";" between the WriteLine and `exit 1`, which
    # would corrupt a naive split). We never touch the network: a fresh
    # single-line verification script is built from these exact literals
    # using the identical decode idiom, and run for real so PowerShell
    # itself (not just base64.b64decode on the Python side) proves the
    # round-trip.
    tokens = re.findall(r"FromBase64String\('([A-Za-z0-9+/=]*)'\)", script_text)
    assert len(tokens) == 3, f"expected 3 base64 literals (share, password, user), got: {tokens!r}"
    _share_b64, pw_b64, user_b64 = tokens
    assert pw_b64 == base64.b64encode(non_ascii_pw.encode("utf-8")).decode("ascii")
    assert user_b64 == base64.b64encode(non_ascii_user.encode("utf-8")).decode("ascii")

    verify_script = (
        f"$p=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{pw_b64}'));"
        f"$u=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{user_b64}'));"
        "$outU=[System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($u));"
        "$outP=[System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($p));"
        "[Console]::Out.Write($outU + '|' + $outP)"
    )
    assert "\n" not in verify_script and "\r" not in verify_script
    verify_script.encode("ascii")  # must still be pure ASCII

    result = real_run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=verify_script.encode("ascii"),
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    out_u_b64, out_p_b64 = result.stdout.decode("ascii").strip().split("|")
    decoded_user = base64.b64decode(out_u_b64).decode("utf-8")
    decoded_pw = base64.b64decode(out_p_b64).decode("utf-8")

    assert decoded_user == non_ascii_user
    assert decoded_pw == non_ascii_pw
    # Verify codepoint-for-codepoint, not just string equality, per the
    # requirement to check decoded codepoints rather than trust the design.
    assert [ord(c) for c in decoded_user] == [ord(c) for c in non_ascii_user]
    assert [ord(c) for c in decoded_pw] == [ord(c) for c in non_ascii_pw]
