"""
Bundles — one sale, several products drawn down.

Shopify reports a bundle as a line item for the bundle SKU. Nothing in that row says the
sale also consumed a vanity, a faucet and a mirror. So without a component map:

  * the parts under-forecast, because the demand that went through the bundle is invisible
  * the parts over-stock on paper, because their inventory falls without matching sales
  * the bundle itself forecasts fine, which is what makes this hard to notice — the
    numbers look plausible everywhere and are wrong in three places

The fix is a map from bundle SKU to components with quantities, and then attribution:
a bundle sale of 2 becomes 2 vanities, 2 faucets and 4 handles, added to whatever those
products sold on their own.

Two deliberate choices:

  1. Attribution is ADDITIVE, never destructive. The bundle's own sales history is kept
     intact — you still want to forecast bundles, and you still want to see where the
     demand came from. Component rows gain units; nothing is rewritten.

  2. Components are flagged, not silently merged. A product whose demand is half bundle
     driven behaves differently from one selling on its own — kill the bundle and half
     its demand vanishes. The share is reported so that's visible rather than buried.
"""

from __future__ import annotations

from collections import defaultdict


class BundleError(ValueError):
    pass


def normalise_map(raw) -> dict:
    """Accept the shapes a user or an API is likely to produce and return
    {bundle_sku: {component_sku: qty_per_bundle}}.

    Tolerated inputs:
        {"KIT-A": {"VAN-1": 1, "FCT-2": 2}}
        {"KIT-A": [{"sku": "VAN-1", "qty": 1}, ...]}
        {"KIT-A": ["VAN-1", "FCT-2"]}            -> qty 1 each
    """
    out = {}
    for bundle, comps in (raw or {}).items():
        b = str(bundle).strip()
        if not b:
            continue
        m = {}
        if isinstance(comps, dict):
            items = comps.items()
        elif isinstance(comps, (list, tuple)):
            items = []
            for c in comps:
                if isinstance(c, str):
                    items.append((c, 1))
                elif isinstance(c, dict) and c.get("sku"):
                    items.append((c["sku"], c.get("qty", c.get("quantity", 1))))
        else:
            continue
        for sku, qty in items:
            s = str(sku).strip()
            if not s or s == b:          # a bundle containing itself would recurse forever
                continue
            try:
                q = float(qty)
            except (TypeError, ValueError):
                q = 1.0
            if q > 0:
                m[s] = m.get(s, 0.0) + q
        if m:
            out[b] = m
    return out


def validate(bundle_map: dict, known_skus=None) -> dict:
    """Report problems rather than raising, so a partly-wrong map still works for the
    parts that are right. A typo'd component should cost you that component, not the
    whole feature."""
    bm = normalise_map(bundle_map)
    known = set(map(str, known_skus)) if known_skus else None
    unknown_bundles, unknown_components, nested = [], [], []
    for b, comps in bm.items():
        if known is not None and b not in known:
            unknown_bundles.append(b)
        for c in comps:
            if known is not None and c not in known:
                unknown_components.append(c)
            if c in bm:
                # A bundle inside a bundle. Supported one level deep by expansion below,
                # but worth surfacing because it's usually a mistake.
                nested.append((b, c))
    return {"bundles": len(bm), "components": len({c for m in bm.values() for c in m}),
            "unknownBundles": sorted(set(unknown_bundles)),
            "unknownComponents": sorted(set(unknown_components)),
            "nested": nested, "ok": not unknown_bundles and not unknown_components}


def expand(bundle_map: dict, max_depth: int = 4) -> dict:
    """Flatten bundles-of-bundles into direct component quantities.

    Depth is bounded and cycles are broken rather than followed: a map that says A
    contains B and B contains A is a user error, and hanging the whole forecast on it
    would be a worse answer than ignoring the loop.
    """
    bm = normalise_map(bundle_map)

    def _resolve(sku, depth, seen):
        if depth >= max_depth or sku in seen:
            return {sku: 1.0}
        comps = bm.get(sku)
        if not comps:
            return {sku: 1.0}
        out = defaultdict(float)
        for c, q in comps.items():
            for leaf, lq in _resolve(c, depth + 1, seen | {sku}).items():
                out[leaf] += q * lq
        return dict(out)

    return {b: _resolve(b, 0, set()) for b in bm}


def attribute(rows: list, bundle_map: dict) -> tuple:
    """Push bundle demand down onto components.

    `rows`: [{date, sku, units_sold, ...}] — the daily sales table.

    Returns (rows_with_component_demand, report). Bundle rows are preserved exactly;
    component rows gain the units that were sold through bundles.
    """
    flat = expand(bundle_map)
    if not flat:
        return list(rows or []), {"bundles": 0, "attributed": 0, "addedUnits": 0.0,
                                  "components": {}, "active": False}

    # date -> sku -> units contributed by bundles
    added = defaultdict(lambda: defaultdict(float))
    per_component = defaultdict(float)
    bundle_units = 0.0
    touched = 0

    for r in rows or []:
        sku = str(r.get("sku"))
        comps = flat.get(sku)
        if not comps:
            continue
        try:
            u = float(r.get("units_sold") or 0)
        except (TypeError, ValueError):
            continue
        if u <= 0:
            continue
        bundle_units += u
        touched += 1
        for c, q in comps.items():
            if c == sku:
                continue
            added[str(r.get("date"))][c] += u * q
            per_component[c] += u * q

    if not added:
        return list(rows or []), {"bundles": len(flat), "attributed": 0, "addedUnits": 0.0,
                                  "components": {}, "active": True}

    out = []
    seen = set()
    for r in rows or []:
        sku = str(r.get("sku"))
        day = str(r.get("date"))
        extra = added.get(day, {}).get(sku, 0.0)
        # Once per (day, component). The table can hold several rows for one product on
        # one day (a sheet with a row per sale, before daily totals are made), and the
        # kit's units used to be added to EVERY one of them.
        if extra and (day, sku) not in seen:
            r = {**r, "units_sold": float(r.get("units_sold") or 0) + extra,
                 "bundle_units": extra}
            seen.add((day, sku))
        out.append(r)

    # A component may have sold ZERO on its own on a day a bundle moved — that day has no
    # row at all, and skipping it would lose the demand entirely.
    for day, comps in added.items():
        for c, extra in comps.items():
            if (day, c) in seen or not extra:
                continue
            out.append({"date": day, "sku": c, "units_sold": extra, "bundle_units": extra,
                        "from_bundle_only": True})

    out.sort(key=lambda r: (str(r.get("date")), str(r.get("sku"))))
    return out, {
        "bundles": len(flat),
        "attributed": touched,
        "bundleUnits": round(bundle_units, 2),
        "addedUnits": round(sum(per_component.values()), 2),
        "components": {k: round(v, 2) for k, v in sorted(per_component.items())},
        "active": True,
    }


def dependency(rows: list, bundle_map: dict) -> dict:
    """What share of each component's demand arrives via bundles.

    The number that matters when someone asks "can we drop this bundle?" — a component at
    70% bundle-driven loses most of its demand the day the bundle stops, and its forecast
    is really a forecast of the bundle.
    """
    attributed, rep = attribute(rows, bundle_map)
    if not rep.get("active"):
        return {}
    totals, from_bundle = defaultdict(float), defaultdict(float)
    for r in attributed:
        sku = str(r.get("sku"))
        totals[sku] += float(r.get("units_sold") or 0)
        from_bundle[sku] += float(r.get("bundle_units") or 0)
    out = {}
    for sku, tot in totals.items():
        if not from_bundle.get(sku):
            continue
        share = from_bundle[sku] / tot * 100 if tot else 0.0
        out[sku] = {
            "totalUnits": round(tot, 2),
            "viaBundles": round(from_bundle[sku], 2),
            "sharePct": round(share, 1),
            # A product mostly sold inside bundles is really a forecast of the bundle.
            "bundleDriven": share >= 50.0,
        }
    return out


def explain(report: dict, dep: dict | None = None) -> str:
    if not report or not report.get("active"):
        return ""
    if not report.get("attributed"):
        return "Bundles are mapped, but none sold in this window."
    parts = [f"{report['bundleUnits']:.0f} bundle sales added "
             f"{report['addedUnits']:.0f} units of demand across "
             f"{len(report['components'])} component products."]
    heavy = [s for s, d in (dep or {}).items() if d.get("bundleDriven")]
    if heavy:
        parts.append(f"{len(heavy)} of them get most of their demand through bundles "
                     f"({', '.join(sorted(heavy)[:3])}"
                     f"{'…' if len(heavy) > 3 else ''}). Forecast those with the bundle in mind.")
    return " ".join(parts)
