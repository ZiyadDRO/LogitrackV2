"""
test_connections.py — the saved-connection store.

The point of these tests is one property: A TOKEN MUST NEVER REACH THE BROWSER. Every
other behaviour here is convenience; that one is the security boundary, so it is asserted
against the serialized public view rather than against individual fields — a leak through
a key nobody thought to check still fails the test.

Run:  python test_connections.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

_TMP = tempfile.mkdtemp()
os.environ["LOGITRACK_CONNECTIONS"] = os.path.join(_TMP, "connections.json")

import connections as C          # noqa: E402 — after the env var is set

SECRET = "EAAAlgTHISMUSTNEVERAPPEAR7Xk2"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def reset():
    if os.path.exists(C.path()):
        os.remove(C.path())


def test_secret_detection():
    print("secret field detection")
    for field in ("token", "accessToken", "access_token", "refreshToken", "clientSecret",
                  "apiKey", "api_key", "password", "userToken", "SECRET"):
        check(f"{field} is secret", C.is_secret(field))
    for field in ("shop", "environment", "locationIds", "days", "label"):
        check(f"{field} is not secret", not C.is_secret(field))


def test_no_leak():
    print("the token never reaches the browser")
    reset()
    pub = C.upsert("square", "Cousin's store",
                   {"accessToken": SECRET, "environment": "production"})
    check("public view omits the token", SECRET not in json.dumps(pub), json.dumps(pub))
    check("public view keeps the non-secret fields",
          pub["creds"]["environment"] == "production")
    check("a masked hint is offered instead",
          pub["secretHint"] == "EAAA••••7Xk2", pub["secretHint"])

    listed = C.list_all()
    check("the listing omits the token too", SECRET not in json.dumps(listed))

    shop = C.upsert("shopify", "My store", {"shop": "acme", "token": "shpat_SECRETVALUE99"})
    check("shopify tokens are masked as well",
          "shpat_SECRETVALUE99" not in json.dumps(shop) and shop["secretHint"] is not None)
    check("shopify keeps its store handle", shop["creds"]["shop"] == "acme")


def test_server_side_get():
    print("server-side reads do get the secret")
    reset()
    pub = C.upsert("square", "Store", {"accessToken": SECRET})
    full = C.get(pub["id"])
    check("get() returns the real token", full["creds"]["accessToken"] == SECRET)
    check("get() on an unknown id is None", C.get("nope") is None)
    check("get() on a blank id is None", C.get("") is None)


def test_update_preserves_secret():
    print("updating without resending the token")
    reset()
    pub = C.upsert("square", "Store", {"accessToken": SECRET, "environment": "production"})
    # This is what the UI sends when someone renames a connection: it never held the token.
    again = C.upsert("square", "Renamed", {"environment": "sandbox"}, connection_id=pub["id"])
    check("the id is stable", again["id"] == pub["id"])
    check("the label changed", again["label"] == "Renamed")
    check("the non-secret field changed", again["creds"]["environment"] == "sandbox")
    check("the token survived", C.get(pub["id"])["creds"]["accessToken"] == SECRET)
    check("only one connection exists", len(C.list_all()) == 1)

    # ...but an explicitly supplied new token DOES replace it.
    C.upsert("square", "Renamed", {"accessToken": "EAAAnewtoken1234"}, connection_id=pub["id"])
    check("a new token replaces the old one",
          C.get(pub["id"])["creds"]["accessToken"] == "EAAAnewtoken1234")


def test_file_permissions():
    print("on-disk permissions")
    reset()
    C.upsert("square", "Store", {"accessToken": SECRET})
    mode = stat.S_IMODE(os.stat(C.path()).st_mode)
    check("the file is owner-only (0600)", mode == 0o600, oct(mode))
    with open(C.path()) as fh:
        check("the token IS on disk (that is the tradeoff)", SECRET in fh.read())


def test_delete_and_touch():
    print("delete + last-used ordering")
    reset()
    a = C.upsert("square", "A", {"accessToken": "EAAAaaaa1111"})
    b = C.upsert("square", "B", {"accessToken": "EAAAbbbb2222"})
    C.touch(a["id"])
    check("most recently used comes first", C.list_all()[0]["id"] == a["id"])
    check("default_for picks it up", C.default_for("square")["id"] == a["id"])
    check("default_for on an unused source is None", C.default_for("shopify") is None)
    check("delete reports success", C.delete(b["id"]) is True)
    check("delete of a missing id reports failure", C.delete(b["id"]) is False)
    check("one connection remains", len(C.list_all()) == 1)


def test_corrupt_file_is_survivable():
    print("a corrupt store does not take the app down")
    reset()
    with open(C.path(), "w") as fh:
        fh.write("{not json at all")
    check("a corrupt file reads as empty", C.list_all() == [])
    pub = C.upsert("square", "Store", {"accessToken": SECRET})
    check("and is overwritten by the next save", len(C.list_all()) == 1 and pub["id"])


if __name__ == "__main__":
    for fn in (test_secret_detection, test_no_leak, test_server_side_get,
               test_update_preserves_secret, test_file_permissions,
               test_delete_and_touch, test_corrupt_file_is_survivable):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All connection-store tests passed.")
