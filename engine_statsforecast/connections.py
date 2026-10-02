"""
connections.py — saved store connections, so a token is entered once rather than on every
launch.

WHY THIS EXISTS

Re-pasting an API token after every restart is friction, but it is also a correctness
problem: the hourly tick that feeds StockLog has to run without anyone present, and
inventory history cannot be backfilled from Shopify — an hour nobody captured is gone.
A connection the server can read on its own is what lets the tick keep running after the
browser is closed.

WHAT IS STORED, AND WHERE IT IS NOT STORED

The access token lives here in plaintext, on the same disk as the catalog, with mode 0600
and a .gitignore entry. That is the same exposure the ROADMAP already assumes when it
tells you to put SHOPIFY_TOKEN in the environment where the backend runs — a local tool
for one operator, not a multi-tenant secret store. It is written down plainly rather than
hidden so the decision is visible when this eventually serves someone else's store.

THE TOKEN IS NEVER SENT TO THE BROWSER. Every read path the API exposes goes through
`public()`, which replaces the secret with a masked hint (EAAA••••7Xk2) that is enough to
recognise a token and useless for calling Square. The frontend therefore cannot leak one
through localStorage, a screenshot, or the network tab.

WHAT THIS IS NOT

Not OAuth. A personal access token is the right tool for one store you control, and the
wrong one for a customer's. When this product connects a stranger's account it needs the
OAuth flow, refresh tokens and expiry handling — at which point `creds` gains
refresh_token / expires_at and this module grows a refresh step. The shape is already
right for that; nothing else in the app reads `creds` directly.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid

_PATH = os.environ.get(
    "LOGITRACK_CONNECTIONS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "connections.json"))

VERSION = 1
_lock = threading.RLock()

# Which credential fields are secret. Anything matching is masked on the way out and
# preserved on an update that doesn't resend it.
#
# This FAILS CLOSED on purpose. An explicit list alone is how a secret leaks: the Square
# source names its token `accessToken`, an explicit list spelled `access_token`, and the
# masking silently did nothing. So the name is also matched against a set of substrings —
# a new source would have to work at it to name a secret field something that slips
# through, and the cost of a false positive (a harmless field gets masked) is a visible
# annoyance rather than an invisible disclosure.
SECRET_FIELDS = {"token", "access_token", "accesstoken", "refresh_token", "refreshtoken",
                 "client_secret", "clientsecret", "api_key", "apikey", "password", "secret"}
_SECRET_SUBSTRINGS = ("token", "secret", "password", "apikey", "api_key", "credential")


def is_secret(field_name: str) -> bool:
    n = (field_name or "").strip().lower()
    if n in SECRET_FIELDS:
        return True
    return any(s in n for s in _SECRET_SUBSTRINGS)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        conns = raw.get("connections")
        if not isinstance(conns, list):
            return {"version": VERSION, "connections": []}
        return {"version": raw.get("version", VERSION), "connections": conns}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
        return {"version": VERSION, "connections": []}


def _save(state: dict) -> bool:
    """Write atomically with owner-only permissions. Best-effort: a disk problem must never
    turn a successful import into a failed one."""
    try:
        tmp = _PATH + ".tmp"
        # Create with 0600 from the start — writing then chmod'ing leaves a window where the
        # token is world-readable, which is the whole thing we are trying to avoid.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, _PATH)
        try:
            os.chmod(_PATH, 0o600)
        except OSError:
            pass
        return True
    except OSError as e:                                 # noqa: BLE001
        print(f"Could not save connections ({e}).")
        return False


def mask(secret) -> str | None:
    """A hint you can recognise and cannot use."""
    if not secret:
        return None
    s = str(secret)
    if len(s) <= 8:
        return "•" * len(s)
    return f"{s[:4]}{'•' * 4}{s[-4:]}"


def public(conn: dict) -> dict:
    """The browser-safe view: everything except the secrets, plus a masked hint."""
    creds = conn.get("creds") or {}
    safe = {k: v for k, v in creds.items() if not is_secret(k)}
    hint = next((mask(v) for k, v in creds.items() if is_secret(k) and v), None)
    return {
        "id": conn.get("id"),
        "source": conn.get("source"),
        "label": conn.get("label"),
        "creds": safe,
        "secretHint": hint,
        "createdAt": conn.get("created_at"),
        "lastUsedAt": conn.get("last_used_at"),
        # In use = synced into the catalog nightly and shown. Paused keeps the login and
        # the store's products (set aside, see workspace.py) but stops the syncing.
        "active": is_active(conn),
    }


def is_active(conn: dict) -> bool:
    """Connections saved before pausing existed have no flag: they are in use."""
    return conn.get("active", True) is not False


def list_all() -> list:
    """Every saved connection, browser-safe, most recently used first."""
    conns = _load()["connections"]
    conns.sort(key=lambda c: (c.get("last_used_at") or c.get("created_at") or ""), reverse=True)
    return [public(c) for c in conns]


def get(connection_id: str) -> dict | None:
    """The full record INCLUDING secrets. Server-side callers only — never returned by an
    endpoint."""
    if not connection_id:
        return None
    for c in _load()["connections"]:
        if c.get("id") == connection_id:
            return c
    return None


def upsert(source: str, label: str, creds: dict, connection_id: str | None = None) -> dict:
    """Create or update a connection. Returns the browser-safe view.

    A secret field that is absent or blank on an update KEEPS the stored value, so the UI
    can re-save a connection (renaming it, changing the lookback) without holding the token
    it was never given.
    """
    creds = dict(creds or {})
    with _lock:
        state = _load()
        existing = None
        for c in state["connections"]:
            if connection_id and c.get("id") == connection_id:
                existing = c
                break
        if existing is not None:
            old = existing.get("creds") or {}
            for k, v in old.items():
                if is_secret(k) and v and not creds.get(k):
                    creds[k] = v
            existing.update({"source": source or existing.get("source"),
                             "label": label or existing.get("label"),
                             "creds": creds})
            record = existing
        else:
            record = {"id": connection_id or uuid.uuid4().hex[:12],
                      "source": source, "label": label, "creds": creds,
                      "created_at": _now(), "last_used_at": None}
            state["connections"].append(record)
        _save(state)
        return public(record)


def list_active() -> list:
    """The connections in use, browser-safe, most recently used first."""
    return [c for c in list_all() if c.get("active")]


def set_active(connection_id: str, active: bool) -> bool:
    """Put a store in use or pause it. Only one store is in use at a time (the catalog
    holds one store's products), so putting one in use pauses the others. Credentials are
    never touched. Returns False when there is no such connection."""
    with _lock:
        state = _load()
        found = False
        for c in state["connections"]:
            if c.get("id") == connection_id:
                c["active"] = bool(active)
                if active:
                    c["last_used_at"] = _now()
                found = True
            elif active:
                c["active"] = False
        if found:
            _save(state)
        return found


def touch(connection_id: str) -> None:
    """Record that a connection was just used, so the UI can offer the obvious one first."""
    with _lock:
        state = _load()
        for c in state["connections"]:
            if c.get("id") == connection_id:
                c["last_used_at"] = _now()
                _save(state)
                return


def delete(connection_id: str) -> bool:
    with _lock:
        state = _load()
        before = len(state["connections"])
        state["connections"] = [c for c in state["connections"] if c.get("id") != connection_id]
        if len(state["connections"]) == before:
            return False
        _save(state)
        return True


def default_for(source: str) -> dict | None:
    """The connection the hourly tick should use when nobody named one: the most recently
    used connection for that source."""
    for c in list_all():
        if c["source"] == source:
            return get(c["id"])
    return None


def path() -> str:
    return _PATH
