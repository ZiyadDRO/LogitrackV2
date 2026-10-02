"""
workspace.py — which products are loaded: a store's, or your spreadsheets.

WHY THIS EXISTS

The app holds one catalog at a time. With a store connected, looking at a spreadsheet
meant either mixing the sheet into the store's products or replacing them, and the
nightly sync would then pull the store back over the sheet at 00:15. The only way out
was to forget the saved login.

So the catalog lives in a WORKSPACE. There is one per saved store ("store-<id>") and one
for spreadsheets ("sheets"). Switching sets the loaded products aside on disk (the saved
catalog plus its measured backtest, exactly as they were persisted) and brings the other
workspace's back. Nothing is deleted, and the store's saved login is not touched: pausing
a store only stops it being synced into the catalog.

Parked copies are plain directory copies of catalog_store/ and backtest_store/, so a
parked workspace survives a restart the same way the live one does.
"""
from __future__ import annotations

import json
import os
import shutil
import threading

_DIR = os.environ.get(
    "LOGITRACK_WORKSPACES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces"))
SHEETS = "sheets"
_lock = threading.RLock()


def store_slot(connection_id: str) -> str:
    return f"store-{connection_id}"


def is_store(slot: str | None) -> bool:
    return bool(slot) and slot.startswith("store-")


def connection_of(slot: str | None) -> str | None:
    return slot[len("store-"):] if is_store(slot) else None


def _slot_dir(slot: str) -> str:
    safe = "".join(ch for ch in str(slot) if ch.isalnum() or ch in "-_")
    return os.path.join(_DIR, safe)


def _state_path() -> str:
    return os.path.join(_DIR, "state.json")


def showing() -> str | None:
    """The workspace currently loaded, or None when it has never been recorded (an
    install from before workspaces existed: the caller works it out from the catalog)."""
    try:
        with open(_state_path()) as fh:
            return (json.load(fh) or {}).get("showing")
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def set_showing(slot: str | None) -> None:
    with _lock:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"showing": slot}, fh)
        os.replace(tmp, _state_path())


def has(slot: str) -> bool:
    d = _slot_dir(slot)
    return os.path.isdir(d) and any(os.scandir(d))


def parked() -> list:
    """Every workspace set aside on disk."""
    if not os.path.isdir(_DIR):
        return []
    return sorted(e.name for e in os.scandir(_DIR) if e.is_dir() and any(os.scandir(e.path)))


def park(slot: str, dirs: list) -> None:
    """Copy the live directories (`dirs` = [(name, path)]) aside under `slot`, replacing
    whatever that slot held. A directory that doesn't exist is simply not copied."""
    with _lock:
        dest = _slot_dir(slot)
        tmp = dest + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        for name, path in dirs:
            if os.path.isdir(path):
                shutil.copytree(path, os.path.join(tmp, name))
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)


def unpark(slot: str, dirs: list) -> bool:
    """Put `slot`'s copies in place of the live directories and forget the parked copy.
    With nothing parked, the live directories are cleared (an empty workspace). Returns
    whether there was anything to bring back."""
    with _lock:
        src = _slot_dir(slot)
        had = has(slot)
        for name, path in dirs:
            shutil.rmtree(path, ignore_errors=True)
            if had and os.path.isdir(os.path.join(src, name)):
                shutil.copytree(os.path.join(src, name), path)
        shutil.rmtree(src, ignore_errors=True)
        return had


def drop(slot: str) -> None:
    with _lock:
        shutil.rmtree(_slot_dir(slot), ignore_errors=True)
