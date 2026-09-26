"""
check_stock.py — reconcile the inventory numbers in the dashboard against Square itself.

WHY THIS EXISTS AND WHY IT DUPLICATES LOGIC ON PURPOSE

The stock figure shown per product is a SUM across every location the connection pulls,
taken as a one-time snapshot. Square's own dashboard shows it PER LOCATION. So the two
disagreeing is the expected case, not evidence of a bug — and that ambiguity is exactly
what makes a quiet error easy to live with for months.

This script answers the question directly: it prints Square's count per location, per SKU,
alongside the total, so a mismatch can be attributed rather than guessed at.

It deliberately does NOT reuse square_source._inventory_counts. A checker that shares the
summing logic with the thing it checks cannot catch a summing bug. It reads the raw API
and adds the numbers up itself.

Usage:
    python check_stock.py                     # the most recently used saved connection
    python check_stock.py --connection <id>
    python check_stock.py --sku MUG-12        # filter
    python check_stock.py --untracked         # only products Square returns no count for
"""
from __future__ import annotations

import sys

import connections as CONN
import square_source as SQ


def _creds(connection_id=None):
    conn = CONN.get(connection_id) if connection_id else CONN.default_for("square")
    if not conn:
        print("No saved Square connection. Connect one in the dashboard, or set "
              "SQUARE_ACCESS_TOKEN in the environment.")
        if not __import__("os").environ.get("SQUARE_ACCESS_TOKEN"):
            sys.exit(1)
        return {}
    c = conn.get("creds") or {}
    print(f"Connection: {conn.get('label')}  ({CONN.mask(c.get('accessToken'))})\n")
    return c


def _raw_counts(token, env, ver, catalog_ids, loc_ids):
    """{(catalog_object_id, location_id): qty} — read straight from the API, summed here
    rather than by the code under test."""
    out = {}
    for i in range(0, len(catalog_ids), 500):
        chunk = catalog_ids[i:i + 500]
        cursor = None
        while True:
            body = {"catalog_object_ids": chunk, "location_ids": loc_ids,
                    "states": ["IN_STOCK"]}
            if cursor:
                body["cursor"] = cursor
            res = SQ._request("POST", "/v2/inventory/counts/batch-retrieve", token, ver, env,
                              json_body=body)
            for c in res.get("counts") or []:
                if (c.get("state") or "IN_STOCK") != "IN_STOCK":
                    continue
                key = (c.get("catalog_object_id"), c.get("location_id"))
                out[key] = out.get(key, 0.0) + float(c.get("quantity") or 0)
            cursor = res.get("cursor")
            if not cursor:
                break
    return out


def main(argv):
    connection_id = None
    sku_filter = None
    untracked_only = "--untracked" in argv
    if "--connection" in argv:
        connection_id = argv[argv.index("--connection") + 1]
    if "--sku" in argv:
        sku_filter = argv[argv.index("--sku") + 1]

    creds = _creds(connection_id)
    token = creds.get("accessToken") or creds.get("access_token")
    env = creds.get("environment") or "production"
    ver = creds.get("apiVersion")
    wanted = creds.get("locationIds") or creds.get("location_ids")

    locations = SQ.fetch_locations(token, env, ver)
    loc_ids = SQ._active_location_ids(locations, wanted)
    loc_name = {l["id"]: l["name"] for l in locations}
    print(f"Locations pulled: {', '.join(loc_name[i] for i in loc_ids)}\n")

    catalog = SQ.fetch_catalog(token, env, ver)
    counts = _raw_counts(token, env, ver, list(catalog), loc_ids)

    # Roll up to the SKU, the same way the importer keys products.
    per_sku = {}
    for cid, info in catalog.items():
        sku = info["sku"]
        row = per_sku.setdefault(sku, {"name": info.get("name") or sku,
                                       "by_loc": {i: None for i in loc_ids},
                                       "has_sku": info.get("has_sku", True)})
        for lid in loc_ids:
            q = counts.get((cid, lid))
            if q is not None:
                row["by_loc"][lid] = (row["by_loc"][lid] or 0) + q

    rows = []
    for sku, r in sorted(per_sku.items()):
        if sku_filter and sku_filter.lower() not in sku.lower():
            continue
        vals = [r["by_loc"][i] for i in loc_ids]
        total = None if all(v is None for v in vals) else sum(v or 0 for v in vals)
        if untracked_only and total is not None:
            continue
        rows.append((sku, r, vals, total))

    wid = max([len(s) for s, *_ in rows] + [8])
    head = f"{'SKU':<{wid}}  {'Product':<34}"
    for i in loc_ids:
        head += f"{loc_name[i][:12]:>14}"
    head += f"{'TOTAL':>9}   note"
    print(head)
    print("-" * len(head))

    tracked = untracked = 0
    for sku, r, vals, total in rows:
        line = f"{sku:<{wid}}  {r['name'][:34]:<34}"
        for v in vals:
            line += f"{('—' if v is None else f'{v:,.0f}'):>14}"
        line += f"{('—' if total is None else f'{total:,.0f}'):>9}"
        notes = []
        if total is None:
            notes.append("NO COUNT — not inventory-tracked in Square")
            untracked += 1
        else:
            tracked += 1
        if not r["has_sku"]:
            notes.append("no SKU set in Square")
        print(line + "   " + "; ".join(notes))

    print()
    print(f"{tracked} product(s) with a Square count, {untracked} without.")
    if untracked:
        print("Products with NO COUNT show the dashboard's 500-unit default, which is "
              "indistinguishable from a real 500. Turn inventory tracking on for them in "
              "Square, or set their stock by hand.")
    if len(loc_ids) > 1:
        print(f"TOTAL is the sum across {len(loc_ids)} locations — Square's own dashboard "
              "shows one location at a time, so compare against the per-location columns.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SQ.SquareError as e:
        print(f"Square: {e}")
        sys.exit(1)
