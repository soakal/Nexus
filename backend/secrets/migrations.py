import pathlib

from cryptography.fernet import Fernet

from .vault import KEY_PATH, set_secret

NON_SECRET_KEYS = {
    "HASS_HOST", "UNIFI_HOST", "UNRAID_HOST", "BRAIN_MCP_URL",
    "CHANNELS_HOST", "ADGUARD_HOST", "HERMES_HOST",
    "BRIEFING_TIME", "BRIEFING_TIMEZONE", "MEMO_WATCH_FOLDER",
    "WHISPER_API", "WHISPER_MODEL", "PR_STALE_HOURS",
    "NEXUS_PORT", "BACKEND_PORT", "DEBUG",
    "WEATHER_LAT", "WEATHER_LON", "GITHUB_USERNAME",
}

def generate_vault_key() -> None:
    if KEY_PATH.exists():
        return
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)

def import_env_file(env_path: str) -> tuple:
    imported = 0
    skipped = 0
    env_file = pathlib.Path(env_path)
    if not env_file.exists():
        raise FileNotFoundError(f"{env_path} not found")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in NON_SECRET_KEYS:
            skipped += 1
            continue
        set_secret(key, value)
        imported += 1
    return imported, skipped
