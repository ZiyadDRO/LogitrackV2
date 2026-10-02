"""Catalog persistence — so a restart doesn't mean a re-upload.

WHAT IS SAVED, AND WHAT DELIBERATELY IS NOT

Only the INGESTED DATA is written to disk: each SKU's daily frame plus the metadata that
came with it (name, attributes, events, source files, and the date-shift offset that
`_true_dates` needs to undo re-anchoring). That is the irreplaceable part — everything
else in the app is derived from it.

Fitted models are NOT saved. Pickling a Prophet or StatsForecast object ties the file to
the exact library versions that wrote it, and a silently-wrong unpickle on a server after
a dependency bump is a far worse failure than a slow boot. Restoring re-fits from the
saved data instead, which is the same code path an upload takes.

Backtest state is NOT saved either, and that is on purpose rather than an omission — see
the "session-scoped by design" note in main.py. Measured protection tiers therefore fall
back to the cost-curve estimate after a restart, which the UI already labels as
provisional. Trigger a re-test from the Backtest tab to measure them again.

FORMAT

CSV plus a JSON manifest, not pickle or parquet: readable, diffable, dependency-free, and
portable across pandas versions. Frames are numbered rather than named after their SKU so
that a product id containing a slash or a colon can't escape the directory.

The whole directory is swapped into place at the end of a save, so a crash mid-write
leaves the previous good copy intact rather than a half-written catalog.
"""

import os, json, shutil, datetime
import pandas as pd

VERSION = 1
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_store")

# Columns a catalog frame may carry, and how to read each one back. Anything not listed
# is dropped on load rather than guessed at.
_DTYPES = {
    "y": "float64",
    "price": "float64",
    "on_promotion": "float64",
    "units_in_stock": "float64",
}


def enabled() -> bool:
    """Set LOGITRACK_PERSIST=0 to run fully in-memory (the old behaviour)."""
    return os.environ.get("LOGITRACK_PERSIST", "1").strip() not in ("0", "false", "no")


def path() -> str:
    return _DIR


def save(catalog: dict, extras: dict | None = None) -> bool:
    """Write the catalog to disk. Returns False on any failure — never raises, because a
    save problem must not fail the upload that triggered it."""
    if not enabled():
        return False
    tmp = _DIR + ".tmp"
    old = _DIR + ".old"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "frames"), exist_ok=True)

        skus = {}
        for i, (sid, entry) in enumerate(catalog.items()):
            df = entry.get("df")
            if df is None or not len(df):
                continue
            out = df.copy()
            out["ds"] = pd.to_datetime(out["ds"]).dt.strftime("%Y-%m-%d")
            keep = ["ds"] + [c for c in _DTYPES if c in out.columns]
            out[keep].to_csv(os.path.join(tmp, "frames", f"{i}.csv"), index=False)
            skus[str(sid)] = {
                "frame": f"{i}.csv",
                "sku_name": entry.get("sku_name"),
                "attrs": entry.get("attrs") or {},
                # Which attributes a person set by hand. Those win over what the store's
                # own data says on a re-sync; the rest follow the store.
                "attrs_set": sorted(entry.get("attrs_set") or []),
                # Which attributes the AI filled in. They never count as set by hand, so
                # the file's or store's own values replace them.
                "attrs_ai": sorted(entry.get("attrs_ai") or []),
                "mode": entry.get("mode") or "uploaded",
                "filename": entry.get("filename"),
                "sources": entry.get("sources") or [],
                "events": entry.get("events") or [],
                "date_shift_days": int(entry.get("date_shift_days") or 0),
                # Whether this SKU came from a live feed. Must survive a restart: without
                # it a later CSV append would re-anchor Shopify data that must keep its
                # real dates.
                "live_source": bool(entry.get("live_source")),
                # The last real stock figure the file gave (None = it gave none). Only
                # written when known, so older saves keep their old behaviour on restore.
                **({"last_known_stock": entry.get("last_known_stock")}
                   if "last_known_stock" in entry else {}),
                "rows": int(len(out)),
            }

        manifest = {
            "version": VERSION,
            "savedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "skus": skus,
            "extras": extras or {},
        }
        with open(os.path.join(tmp, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)

        # Swap: the live directory is replaced only once the new one is complete.
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(_DIR):
            os.rename(_DIR, old)
        os.rename(tmp, _DIR)
        shutil.rmtree(old, ignore_errors=True)
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f"[catalog_store] save failed ({e}) — continuing in memory only")
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def load() -> tuple[dict, dict]:
    """Return (catalog, extras). Empty dicts when there is nothing to restore, when
    persistence is switched off, or when the saved copy can't be read."""
    if not enabled() or not os.path.isfile(os.path.join(_DIR, "manifest.json")):
        return {}, {}
    try:
        with open(os.path.join(_DIR, "manifest.json")) as f:
            manifest = json.load(f)
        if int(manifest.get("version") or 0) != VERSION:
            print(f"[catalog_store] saved catalog is version {manifest.get('version')}, "
                  f"this build reads {VERSION} — ignoring it")
            return {}, {}

        catalog = {}
        for sid, meta in (manifest.get("skus") or {}).items():
            fp = os.path.join(_DIR, "frames", meta.get("frame", ""))
            if not os.path.isfile(fp):
                print(f"[catalog_store] {sid}: frame missing, skipped")
                continue
            df = pd.read_csv(fp)
            df["ds"] = pd.to_datetime(df["ds"])
            for col, dt in _DTYPES.items():
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype(dt)
            catalog[sid] = {
                "df": df.sort_values("ds").reset_index(drop=True),
                "attrs": meta.get("attrs") or {},
                "attrs_set": list(meta.get("attrs_set") or []),
                "attrs_ai": list(meta.get("attrs_ai") or []),
                "sku_name": meta.get("sku_name") or sid,
                "mode": meta.get("mode") or "uploaded",
                "filename": meta.get("filename"),
                "sources": meta.get("sources") or [],
                "events": meta.get("events") or [],
                "date_shift_days": int(meta.get("date_shift_days") or 0),
                "live_source": bool(meta.get("live_source")),
                **({"last_known_stock": meta.get("last_known_stock")}
                   if "last_known_stock" in meta else {}),
            }
        return catalog, (manifest.get("extras") or {})
    except Exception as e:                                  # noqa: BLE001
        print(f"[catalog_store] restore failed ({e}) — starting empty")
        return {}, {}


def clear() -> None:
    """Forget the saved catalog. Called by /api/reset, so that clearing the app and
    restarting doesn't bring the data back from disk."""
    shutil.rmtree(_DIR, ignore_errors=True)
    shutil.rmtree(_DIR + ".tmp", ignore_errors=True)
    shutil.rmtree(_DIR + ".old", ignore_errors=True)
