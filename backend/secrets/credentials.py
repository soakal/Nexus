"""Shared shape of the namespaced `cred:<service>:<field>` secret keys.

Pure helpers (no I/O, no backend imports) so both secret backends —
`vault` (encrypted JSON file) and `infisical_client` (remote store + cache) —
derive the same key names and build the same credential views from whatever
key/value access their storage happens to offer.
"""

CRED_PREFIX = "cred:"

# Every field a credential can carry. `password` is deliberately excluded from
# CRED_PUBLIC_FIELDS: list_credentials() reports only its presence, never its value.
CRED_FIELDS = ("host", "user", "password", "port")
CRED_PUBLIC_FIELDS = ("host", "user", "port")


def cred_key(service: str, field: str) -> str:
    """Storage key for one credential field."""
    return f"{CRED_PREFIX}{service}:{field}"


def cred_prefix(service: str) -> str:
    """Key prefix shared by every field of one service's credential."""
    return f"{CRED_PREFIX}{service}:"


def parse_cred_key(raw_key: str) -> tuple[str, str] | None:
    """Split a `cred:<service>:<field>` key into (service, field).

    Returns None for any key that isn't a well-formed credential key, so
    callers can filter a mixed keyspace with a single check.
    """
    if not raw_key.startswith(CRED_PREFIX):
        return None
    parts = raw_key.split(":", 2)
    if len(parts) != 3:
        return None
    _, service, field = parts
    return service, field


def build_credential_index(keys, get_value) -> dict:
    """Build {service: {host, user, port, has_password}} from a key iterable.

    `get_value(raw_key)` is called only for the non-secret fields — a password
    contributes `has_password: True` and its value is never read.
    """
    result: dict = {}
    for raw_key in keys:
        parsed = parse_cred_key(raw_key)
        if parsed is None:
            continue
        service, field = parsed
        entry = result.setdefault(
            service, {"host": None, "user": None, "port": None, "has_password": False}
        )
        if field == "password":
            entry["has_password"] = True
        elif field in CRED_PUBLIC_FIELDS:
            entry[field] = get_value(raw_key)
    return result


def collect_credential(service: str, get_secret) -> dict:
    """Read every field of one service's credential, password included.

    A field the backend doesn't have (KeyError) comes back as None rather than
    raising, so a partially configured service still returns a full dict.
    """
    result = {}
    for field in CRED_FIELDS:
        try:
            result[field] = get_secret(cred_key(service, field))
        except KeyError:
            result[field] = None
    return result
