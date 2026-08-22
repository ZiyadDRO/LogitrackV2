"""
router.py — decides WHICH forecasting method each SKU should use.

Client-agnostic. The router never knows or cares what the products are; it only
looks at three things per SKU:

  1. How much of its OWN history it has (days + total units).
  2. What its demand PATTERN looks like (regular vs intermittent).
  3. How many RELATED SKUs it has to borrow from (pooling potential).

and maps those to one of four routes:

  • "prophet"   — established SKU with enough regular history. The workhorse.
  • "global"    — new / thin / sparse, BUT has enough related SKUs to pool from.
                  (This is the lazy, conditional case: only fires when there's a
                   relatively under-served product AND enough data to build
                   assumptions from — exactly the trigger you described.)
  • "croston"   — intermittent/lumpy demand with a real track record of its own
                  (≥ NEW_DAYS) — even when relatives exist, since pooling would
                  apply the category's typical volume and over-forecast a
                  slow-mover. Also the fallback for thin intermittent orphans.
  • "abstain"   — too little signal of any kind; don't fake a number.

Relatedness is generic: it groups SKUs by whatever categorical signal the upload
happens to carry (a Category column, or brand/size/type/… columns), and falls
back to a Groq-extracted attribute map when only free-text names exist. No
attribute names are hard-coded, so it works for any catalog.
"""

from __future__ import annotations
import pandas as pd
import numpy as np

# ── Thresholds (all tunable; sensible universal defaults) ────────────────────
ESTABLISHED_DAYS   = 180   # "enough regular history" for Prophet
ESTABLISHED_SELLING_DAYS = 45  # repeated sales observations, not one bulk spike
ESTABLISHED_SALES  = 60        # low guardrail; selling-days carries the real evidence
NEW_DAYS           = 60    # below this, a SKU is "new/thin" (its own history is weak)
MIN_HISTORY_DAYS   = 90    # below this AND no relatives → abstain
MIN_HISTORY_SALES  = 30
MIN_RELATIVES      = 2     # related SKUs (each with usable history) needed to pool
                           # (2 solid category-mates is enough to borrow a seasonal shape;
                           #  the young/baseline gates still mute a brand-new SKU's display)
RELATIVE_MIN_DAYS  = 180   # a SKU only counts as a usable "relative" with ≥ this history
MAX_ADAPTIVE_DEPTH = 3     # deepest automatic subgroup, e.g. Category | Size | Finish | Brand
MIN_SPLIT_GAIN     = 0.08  # narrower group must improve behaviour vs parent by this much
MIN_GROUP_COHESION = 0.35  # broad/narrow pools below this are too incoherent to trust

# Columns that are never treated as grouping/attribute signal.
# cost/unit_cost are unit ECONOMICS (they seed the Scorecard's cost field on
# upload), not product attributes — grouping on them would be meaningless.
RESERVED_COLS = {"ds", "y", "date", "units_sold", "price", "on_promotion",
                 "units_in_stock", "sku", "sku_name", "cost", "unit_cost"}

# Within a broad category, prefer fields that describe what the product *is*
# before fields that describe incidental traits. This keeps a "floating vanity"
# pool from being explained as "same hardware finish", and keeps single-sink
# vanities from fragmenting by material unless material truly adds signal.
FAMILY_SPLIT_PRIORITY = {
    "subcategory": 0, "sub_category": 0,
    "product_type": 1, "type": 1, "family": 1, "product_family": 1,
    "class": 2, "collection": 2,
    "style": 3, "vanity_type": 3,
    "mounting": 4, "mount_type": 4,
}
INCIDENTAL_SPLIT_COLS = {
    "color", "colour", "finish", "material", "top_material", "hardware_finish",
    "size", "dimensions", "width", "width_in", "height", "depth", "weight",
    "sink_count",
}


def _split_col_rank(col: str) -> int:
    return FAMILY_SPLIT_PRIORITY.get(str(col).lower(), 20 if str(col).lower() in INCIDENTAL_SPLIT_COLS else 10)


def _combo_has_family_signal(combo) -> bool:
    return any(_split_col_rank(c) < 10 for c in combo)


# ─────────────────────────────────────────────────────────────────────────────
#  RELATEDNESS  (universal grouping)
# ─────────────────────────────────────────────────────────────────────────────
def detect_group_columns(catalog: dict) -> list[str]:
    """Pick the categorical column(s) to group on, from whatever the upload has.
    Priority goes to obvious category-like names; otherwise any non-reserved
    column that looks categorical (few distinct values relative to catalog size).
    Returns [] if nothing usable is present (→ Groq fallback or orphans)."""
    # Gather attribute columns present on ANY SKU (union — a category column that
    # only some SKUs carry is still a valid grouping signal; SKUs without it just
    # become orphans).
    attr_keys = set()
    for entry in catalog.values():
        attr_keys |= set((entry.get("attrs") or {}).keys()) - RESERVED_COLS

    priority = ["category", "subcategory", "type", "product_type", "family", "brand"]
    for p in priority:
        if p in attr_keys:
            return [p]
    # Fall back: the single attribute with the most "groupable" cardinality
    # (more than one group, but not all-unique). BUT never group on a superficial,
    # cross-category trait — colour/size/material/style describe many unrelated products
    # (a black chair, black laptop and black pencil are NOT similar), so grouping on one
    # of those produces meaningless pools. We only group on attributes that imply a real
    # product family; if none exist we return [] (orphans) rather than a bogus group.
    SUPERFICIAL = {"color", "colour", "size", "material", "style", "weight", "dimensions"}
    n = max(len(catalog), 1)
    best, best_score = None, -1.0
    for k in attr_keys:
        if k in SUPERFICIAL:
            continue
        vals = [str((e.get("attrs") or {}).get(k)) for e in catalog.values() if (e.get("attrs") or {}).get(k) is not None]
        if not vals:
            continue
        distinct = len(set(vals))
        if 1 < distinct < n:                      # genuinely groups things
            score = sum(1 for v in vals) / distinct  # avg members per group
            if score > best_score:
                best, best_score = k, score
    return [best] if best else []


# Catch-all / placeholder category values that don't describe a real product family.
# A SKU labelled with one of these is treated as ungrouped — pooling a "Core" or "Misc"
# bucket lumps unrelated products (a glove, a notebook, a mug) together, which is exactly
# what we don't want. Matched case-insensitively against the whole group value.
JUNK_GROUP_VALUES = {
    "core", "misc", "miscellaneous", "other", "others", "general", "generic", "default",
    "uncategorized", "uncategorised", "none", "null", "n/a", "na", "various", "assorted",
    "unknown", "tbd", "item", "items", "product", "products", "sku", "new", "sale",
    "clearance", "everything else", "no category", "unsorted",
}


def group_catalog(catalog: dict, group_cols: list[str] | None = None) -> dict:
    """Returns {sku_id: group_key}. group_key is None for orphans (no group).
    `group_cols` overrides auto-detection (e.g. ['category'])."""
    cols = group_cols if group_cols is not None else detect_group_columns(catalog)
    groups: dict = {}
    if not cols:
        return {sku: None for sku in catalog}     # nothing to group on → all orphans
    for sku, entry in catalog.items():
        attrs = entry.get("attrs") or {}
        vals = [str(attrs.get(c)) for c in cols if attrs.get(c) is not None]
        key = " | ".join(vals) if len(vals) == len(cols) else None
        if key is not None and key.strip().lower() in JUNK_GROUP_VALUES:
            key = None                            # placeholder category → not a real group
        groups[sku] = key
    return groups


def _attr_value(entry: dict, key: str):
    v = (entry.get("attrs") or {}).get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in JUNK_GROUP_VALUES else None


def _usable_member_ids(member_ids, catalog: dict, min_days: int = RELATIVE_MIN_DAYS) -> list:
    out = []
    for sid in member_ids:
        df = catalog.get(sid, {}).get("df")
        if df is not None and len(df) > 1 and _days(df) >= min_days and _seasonal_signature(df) is not None:
            out.append(sid)
    return out


def _cohesion_for_ids(member_ids, catalog: dict) -> float:
    sigs = [s for s in (_seasonal_signature(catalog[m]["df"]) for m in member_ids if m in catalog) if s is not None]
    if len(sigs) < 2:
        return 0.0
    vals = [_shape_corr(sigs[i], sigs[j]) for i in range(len(sigs)) for j in range(i + 1, len(sigs))]
    return float(np.mean(vals)) if vals else 0.0


def _adaptive_attr_columns(catalog: dict, base_cols: list[str]) -> list[str]:
    """Attributes worth trying as WITHIN-family split candidates.
    Unlike broad grouping, size/color/material are allowed here because the base
    family already prevents nonsense like grouping a black chair with a black mug."""
    keys = set()
    for entry in catalog.values():
        keys |= set((entry.get("attrs") or {}).keys()) - RESERVED_COLS
    keys -= set(base_cols or [])
    # Identifiers and pure one-offs fragment pools without adding reusable signal.
    blocked = {"id", "sku", "barcode", "upc", "mpn", "gtin", "description", "title", "name"}
    keys = {k for k in keys if k not in blocked}
    n = max(len(catalog), 1)
    scored = []
    for k in keys:
        vals = [_attr_value(e, k) for e in catalog.values()]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        distinct = len(set(vals))
        # Must group at least something, but must not be almost all-unique.
        if 1 < distinct <= max(2, int(n * 0.75)):
            scored.append((_split_col_rank(k), -(len(vals) / distinct), distinct, k))
    return [k for *_, k in sorted(scored)]


def _candidate_combos(attrs: dict, split_cols: list[str]) -> list[tuple[str, ...]]:
    present = [c for c in split_cols if attrs.get(c) is not None]
    combos = []
    for depth in range(1, min(MAX_ADAPTIVE_DEPTH, len(present)) + 1):
        # Local import keeps router lightweight and avoids a global dependency for tests.
        from itertools import combinations
        combos.extend(combinations(present, depth))
    # Deeper first, then stable lexical order.
    return sorted(combos, key=lambda c: (-len(c), c))


def adaptive_group_catalog(catalog: dict, base_cols: list[str] | None = None) -> tuple[dict, dict]:
    """Pick the most specific trustworthy group for each SKU.

    Starts with a broad family/category, then evaluates attribute subgroups such as
    Category|Size, Category|Material, or Category|Size|Material. A narrower group is
    used only when it has enough established members AND improves behavioural
    cohesion over its parent. If no candidate earns it, the SKU falls back to the
    broad family. Returns (groups, meta_by_sku).
    """
    base_cols = base_cols if base_cols is not None else detect_group_columns(catalog)
    base_groups = group_catalog(catalog, base_cols)
    split_cols = _adaptive_attr_columns(catalog, base_cols)
    meta = {}
    if not split_cols:
        return base_groups, {sid: {"level": "base", "groupCols": base_cols, "baseGroup": g,
                                   "reason": "No reliable subgroup attributes were available."}
                             for sid, g in base_groups.items()}

    # Precompute candidate membership and scores within each broad group.
    members_by_base = {}
    for sid, g in base_groups.items():
        if g is not None:
            members_by_base.setdefault(g, []).append(sid)

    candidate_scores: dict[tuple[str, tuple[str, ...], tuple[str, ...]], dict] = {}
    base_scores: dict[str, dict] = {}
    for base, members in members_by_base.items():
        usable_base = _usable_member_ids(members, catalog)
        base_coh = _cohesion_for_ids(usable_base, catalog)
        base_scores[base] = {"usable": usable_base, "cohesion": base_coh}
        for sid in members:
            attrs = catalog[sid].get("attrs") or {}
            clean_attrs = {k: _attr_value(catalog[sid], k) for k in split_cols}
            clean_attrs = {k: v for k, v in clean_attrs.items() if v is not None}
            for combo in _candidate_combos(clean_attrs, split_cols):
                vals = tuple(clean_attrs[c] for c in combo)
                key = (base, combo, vals)
                bucket = candidate_scores.setdefault(key, {"members": []})
                bucket["members"].append(sid)

    for key, d in list(candidate_scores.items()):
        base, combo, vals = key
        usable = _usable_member_ids(d["members"], catalog)
        coh = _cohesion_for_ids(usable, catalog)
        d.update({"usable": usable, "cohesion": coh})

    for key, d in list(candidate_scores.items()):
        base, combo, vals = key
        parent_coh = base_scores.get(base, {}).get("cohesion", 0.0)
        if len(combo) > 1:
            from itertools import combinations
            value_by_col = dict(zip(combo, vals))
            for depth in range(1, len(combo)):
                for parent_combo in combinations(combo, depth):
                    parent_vals = tuple(value_by_col[c] for c in parent_combo)
                    pd = candidate_scores.get((base, parent_combo, parent_vals))
                    if pd and len(pd.get("usable", [])) >= MIN_RELATIVES:
                        parent_coh = max(parent_coh, pd.get("cohesion", 0.0))
        has_family_signal = _combo_has_family_signal(combo)
        min_gain = MIN_SPLIT_GAIN if has_family_signal else MIN_SPLIT_GAIN + 0.12
        if not has_family_signal and len(base_scores.get(base, {}).get("usable", [])) >= MIN_RELATIVES:
            min_gain = max(min_gain, 0.18)
        d.update({
            "gain": d["cohesion"] - parent_coh,
            "passes": len(d["usable"]) >= MIN_RELATIVES and d["cohesion"] >= MIN_GROUP_COHESION and (d["cohesion"] - parent_coh) >= min_gain,
            "groupKey": " | ".join([base] + [f"{c}:{v}" for c, v in zip(combo, vals)]),
            "groupCols": list(base_cols or []) + list(combo),
            "values": vals,
            "hasFamilySignal": has_family_signal,
            "minGain": min_gain,
        })

    groups = {}
    for sid, base in base_groups.items():
        if base is None:
            groups[sid] = None
            meta[sid] = {"level": "none", "groupCols": base_cols, "baseGroup": None,
                         "reason": "No broad product family/category was available."}
            continue
        attrs = catalog[sid].get("attrs") or {}
        clean_attrs = {k: _attr_value(catalog[sid], k) for k in split_cols}
        clean_attrs = {k: v for k, v in clean_attrs.items() if v is not None}
        passing = []
        considered = 0
        for combo in _candidate_combos(clean_attrs, split_cols):
            vals = tuple(clean_attrs[c] for c in combo)
            d = candidate_scores.get((base, combo, vals))
            if not d:
                continue
            considered += 1
            if d.get("passes"):
                # Prefer product-family fields, then larger support, then stronger
                # behaviour. Specific incidental refinements are last-resort tie-breakers.
                family_score = 100 - min(_split_col_rank(c) for c in combo)
                passing.append((family_score, len(d["usable"]), d["cohesion"], d["gain"], -len(combo), d))
        if passing:
            passing.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
            d = passing[0][-1]
            groups[sid] = d["groupKey"]
            meta[sid] = {
                "level": "subgroup", "baseGroup": base, "groupCols": d["groupCols"],
                "splitCols": list(d["groupCols"][len(base_cols or []):]),
                "usableMembers": len(d["usable"]), "cohesion": round(d["cohesion"], 3),
                "gain": round(d["gain"], 3),
                "reason": (f"Used the most specific reliable subgroup: {len(d['usable'])} established "
                           f"matches and stronger behaviour than the broad group."),
            }
        else:
            groups[sid] = base
            bs = base_scores.get(base, {})
            meta[sid] = {
                "level": "base", "baseGroup": base, "groupCols": base_cols,
                "usableMembers": len(bs.get("usable", [])), "cohesion": round(bs.get("cohesion", 0.0), 3),
                "reason": ("Narrower attribute matches were too small or did not behave more coherently, "
                           "so this product falls back to the broader reliable group."
                           if considered else "No matching subgroup attributes were available; using the broader group."),
            }
    return groups, meta


def count_usable_relatives(sku_id: str, groups: dict, catalog: dict,
                           min_days: int = RELATIVE_MIN_DAYS) -> int:
    """How many OTHER SKUs share this SKU's group AND have enough history to be
    worth borrowing from."""
    g = groups.get(sku_id)
    if g is None:
        return 0
    n = 0
    for other, og in groups.items():
        if other == sku_id or og != g:
            continue
        df = catalog[other].get("df")
        if df is not None and len(df) >= 2 and _days(df) >= min_days:
            n += 1
    return n


def related_frames(sku_id: str, groups: dict, catalog: dict,
                   min_days: int = RELATIVE_MIN_DAYS) -> list[pd.DataFrame]:
    """The donor history frames the global model pools over."""
    g = groups.get(sku_id)
    out = []
    if g is None:
        return out
    for other, og in groups.items():
        if other == sku_id or og != g:
            continue
        df = catalog[other].get("df")
        if df is not None and len(df) >= 2 and _days(df) >= min_days:
            out.append(df)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  BEHAVIOURAL PEER GROUPING
#  A shared category label is a coarse signal — a category can mix products that
#  behave very differently (a summer-peaking and a winter-peaking item, a steady
#  seller and a spiky one). Pooling a brand-new SKU across all of them muddies the
#  borrowed seasonal SHAPE. So inside each category we cluster the established
#  members by how similar their seasonal pattern actually is, and a new SKU only
#  borrows from the cluster it best fits — "relatives that genuinely behave alike,"
#  not just "relatives that share a label."
# ─────────────────────────────────────────────────────────────────────────────
SHAPE_MIN_DAYS  = 120    # history needed before a SKU has a stable seasonal signature
SHAPE_THRESHOLD = 0.40   # min weekly+monthly correlation to call two SKUs "alike"


def _seasonal_signature(df, min_days: int = SHAPE_MIN_DAYS):
    """Scale-free seasonal fingerprint: the normalized weekly(7) + monthly(12) profile.
    Each entry is the average of (day ÷ overall mean) for that weekday / month, so the
    fingerprint is independent of volume — a 5/day and a 500/day item with the same
    rhythm produce the same vector. Returns None if there isn't enough history."""
    d = df.dropna(subset=["y"]) if "y" in getattr(df, "columns", []) else df
    if d is None or len(d) < min_days:
        return None
    y = d["y"].to_numpy(dtype=float)
    mu = float(y.mean()) or 1.0
    norm = y / mu
    wd = d["ds"].dt.weekday.to_numpy(); mn = d["ds"].dt.month.to_numpy() - 1
    wk = np.array([norm[wd == i].mean() if (wd == i).any() else 1.0 for i in range(7)])
    mo = np.array([norm[mn == i].mean() if (mn == i).any() else 1.0 for i in range(12)])
    return np.concatenate([wk, mo])


def _shape_corr(a, b) -> float:
    """Correlation between two seasonal fingerprints, robust to flat (no-season) ones."""
    if a is None or b is None:
        return 0.0
    fa, fb = np.std(a) < 1e-9, np.std(b) < 1e-9
    if fa or fb:
        return 1.0 if (fa and fb) else 0.0   # two flat profiles are 'alike'; flat vs seasonal isn't
    c = float(np.corrcoef(a, b)[0, 1])
    return c if np.isfinite(c) else 0.0


def _cluster_by_shape(items, threshold: float = SHAPE_THRESHOLD):
    """items: list of (key, signature). Connected-components clustering — any two SKUs
    whose fingerprints correlate ≥ threshold land in the same cluster."""
    n = len(items); parent = list(range(n))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            if _shape_corr(items[i][1], items[j][1]) >= threshold:
                parent[find(i)] = find(j)
    buckets: dict = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(items[i][0])
    return list(buckets.values())


def cluster_catalog(groups: dict, catalog: dict, min_days: int = RELATIVE_MIN_DAYS,
                    threshold: float = SHAPE_THRESHOLD) -> dict:
    """For every category, cluster its usable members (≥ min_days history) by seasonal
    shape. Returns {group_key: [[sku_id, ...], ...]}."""
    by_group: dict = {}
    for sid, g in groups.items():
        if g is None:
            continue
        df = catalog[sid].get("df")
        if df is not None and len(df) > 1 and _days(df) >= min_days:
            by_group.setdefault(g, []).append(sid)
    out: dict = {}
    for g, members in by_group.items():
        sigs = [(m, _seasonal_signature(catalog[m]["df"])) for m in members]
        sigs = [(m, s) for m, s in sigs if s is not None]
        out[g] = ([[m for m, _ in sigs]] if sigs else []) if len(sigs) <= 1 \
            else _cluster_by_shape(sigs, threshold)
    return out


def cluster_cohesion(member_ids, catalog, baseline=None) -> dict | None:
    """How alike a cluster's members are, as {avg, min, distinctive} (each 0–1) or None.
      • avg / min — raw pairwise correlation of the members' seasonal fingerprints
        (avg = overall agreement, min = the weakest pair, so one odd member can't hide).
      • distinctive — correlation AFTER subtracting the catalog-wide baseline shape, i.e.
        how much they co-move BEYOND the generic retail rhythm everything shares. This is
        the trustworthy number: a high `avg` with a low `distinctive` means they only look
        similar because both follow normal seasonality, not because they're truly alike."""
    sigs = [s for s in (_seasonal_signature(catalog[m]["df"]) for m in member_ids if m in catalog) if s is not None]
    if len(sigs) < 2:
        return None
    raw = [_shape_corr(sigs[i], sigs[j]) for i in range(len(sigs)) for j in range(i + 1, len(sigs))]
    out = {"avg": round(float(np.mean(raw)), 3), "min": round(float(np.min(raw)), 3)}
    if baseline is not None:
        bl = np.asarray(baseline, float)
        res = [np.asarray(s, float) - bl for s in sigs]
        dist = [_shape_corr(res[i], res[j]) for i in range(len(res)) for j in range(i + 1, len(res))]
        out["distinctive"] = round(float(np.mean(dist)), 3) if dist else None
        # Plain-language drivers: the months/weekend where this group's average shape
        # deviates most from the catalog norm (what makes them distinctively alike).
        dev = np.mean(sigs, axis=0) - bl
        TH = 0.10
        mo = dev[7:19]
        hi = [_MONTHS[i] for i in np.argsort(mo)[::-1] if mo[i] > TH][:2]
        lo = [_MONTHS[i] for i in np.argsort(mo) if mo[i] < -TH][:2]
        we = float((dev[5] + dev[6]) / 2)   # Sat+Sun deviation
        out["shared"] = {"highMonths": hi, "lowMonths": lo,
                         "weekend": "high" if we > TH else "low" if we < -TH else None}
    return out


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


def behavioral_relatives(sku_id: str, groups: dict, catalog: dict,
                         clusters: dict | None = None, min_days: int = RELATIVE_MIN_DAYS):
    """The donor frames a SKU should actually pool from: the single behavioural cluster
    (within its category) it best fits. Returns (frames, n, info).

    Cluster choice for the target: by its own seasonal shape if it has enough history;
    else by sales-volume proximity; else the largest cluster. info carries the clusters
    and the basis, for display."""
    g = groups.get(sku_id)
    if g is None:
        return [], 0, None
    if clusters is None:
        clusters = cluster_catalog(groups, catalog, min_days=min_days)
    cls = [[m for m in cl if m != sku_id] for cl in clusters.get(g, [])]
    cls = [cl for cl in cls if cl]
    if not cls:
        return [], 0, None
    if len(cls) == 1:
        chosen = cls[0]
        return ([catalog[m]["df"] for m in chosen], len(chosen),
                {"clusters": [list(c) for c in cls], "chosen": list(chosen), "basis": "category is behaviourally coherent"})

    # For genuinely new/thin products, do NOT let a tiny amount of own behavior
    # steer them into a one-SKU veteran cluster. The point of the global model is
    # to borrow seasonality from multiple established siblings; a singleton match
    # cannot provide that pool. Prefer clusters that meet the pooling minimum.
    eligible_cls = [cl for cl in cls if len(cl) >= MIN_RELATIVES]
    candidate_cls = eligible_cls or cls

    def centroid(cl):
        sigs = [s for s in (_seasonal_signature(catalog[m]["df"]) for m in cl) if s is not None]
        return np.mean(sigs, axis=0) if sigs else None

    tgt_df = catalog[sku_id].get("df")
    tgt_days = _days(tgt_df) if tgt_df is not None and len(tgt_df) > 1 else 0
    tgt_sig = _seasonal_signature(tgt_df, min_days=28)
    if tgt_sig is not None and tgt_days >= NEW_DAYS:
        chosen = max(candidate_cls, key=lambda cl: _shape_corr(tgt_sig, centroid(cl)))
        basis = "matched by its own seasonal shape"
    else:
        own = tgt_df.dropna(subset=["y"])["y"] if tgt_df is not None else []
        tgt_mean = float(own.mean()) if len(own) else None
        if tgt_mean and tgt_mean > 0:
            def cl_vol(cl):
                vols = [float(catalog[m]["df"]["y"].mean()) for m in cl]
                return float(np.median(vols)) if vols else 1.0
            chosen = min(candidate_cls, key=lambda cl: abs(np.log((cl_vol(cl) or 1.0) / tgt_mean)))
            basis = ("matched to the closest multi-product seasonal pool by sales volume "
                     "(too new for its own seasonal shape)") if eligible_cls else "matched by sales volume (too new for a shape)"
        else:
            chosen = max(candidate_cls, key=len)
            basis = ("defaulted to the largest multi-product seasonal pool (no own signal yet)"
                     if eligible_cls else "defaulted to the largest cluster (no own signal yet)")
    return ([catalog[m]["df"] for m in chosen], len(chosen),
            {"clusters": [list(c) for c in cls], "chosen": list(chosen), "basis": basis})


def _days(df: pd.DataFrame) -> int:
    return int((df["ds"].max() - df["ds"].min()).days) if len(df) > 1 else 0


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTE DECISION
# ─────────────────────────────────────────────────────────────────────────────
def route(days_history: int, total_sales: int, demand_class: str,
          n_relatives: int, selling_days: int | None = None) -> tuple[str, str]:
    """Returns (method, plain_language_reason). method ∈ prophet|global|croston|abstain."""
    intermittent = demand_class in ("intermittent", "lumpy")   # sells sporadically, but DOES sell
    sparse = intermittent or demand_class == "no_demand"
    # Repeated observations beat raw unit count. A single bulk order of 100 units is
    # not "established"; a lower-volume SKU selling on many separate days is.
    if selling_days is None:
        selling_days = min(int(total_sales), int(days_history)) if total_sales else 0
    established = (
        days_history >= ESTABLISHED_DAYS
        and selling_days >= ESTABLISHED_SELLING_DAYS
        and total_sales >= ESTABLISHED_SALES
    )
    is_new = days_history < NEW_DAYS

    # 1) Established, regular demand → Prophet (the main workhorse).
    if established and not sparse:
        return "prophet", (
            f"This product has a solid run of its own history (sales spanning {days_history} days, "
            f"{total_sales:,} units sold across {selling_days:,} selling days) with a regular sales "
            f"pattern, so it's forecast directly with Prophet.")

    # 2) Genuinely INTERMITTENT/LUMPY demand with enough of its OWN history → Croston/TSB.
    #    Croston is purpose-built for sporadic demand. Pooling such an item into the global
    #    model would borrow the category's TYPICAL VOLUME and badly over-forecast a slow-mover,
    #    so a sporadic product with a real track record uses Croston even when relatives exist.
    if intermittent and days_history >= NEW_DAYS:
        return "croston", (
            f"Demand is intermittent (sells sporadically, with many zero-sale days) and there's "
            f"enough of its own history ({days_history} days) to model that pattern, so a specialist "
            f"intermittent-demand model (Croston/TSB) is used — rather than a pooled model that "
            f"would apply the category's typical volume and over-forecast a slow-mover.")

    # 3) New / thin / no-own-sales, but enough related products to learn from → global pooled.
    #    (A NEW sparse SKU can't yet be told apart from a genuinely intermittent one, so it
    #    borrows from peers; a no-demand SKU has nothing for Croston to model.)
    if n_relatives >= MIN_RELATIVES and (is_new or sparse or not established):
        why_thin = ("it's a new product with little history of its own" if is_new
                    else "it has no sales of its own yet" if demand_class == "no_demand"
                    else "its own demand is too sparse to model alone" if sparse
                    else ("it has a long calendar history but not enough repeated selling days "
                          "to trust its own seasonal pattern yet") if days_history >= ESTABLISHED_DAYS
                    else "its own history is still thin")
        return "global", (
            f"Because {why_thin}, it borrows the seasonal shape and typical volume from "
            f"{n_relatives} related products with established history (pooled / global model).")

    # 4) Intermittent demand we couldn't route above (thin history AND no relatives) → Croston anyway.
    if intermittent:
        return "croston", (
            "Demand is intermittent (lots of zero-sale days) and there aren't enough similar "
            "products to pool from, so a specialist intermittent-demand model (Croston/TSB) is used.")

    # 5) Not enough signal of any kind → abstain (last-resort moving average).
    if days_history < MIN_HISTORY_DAYS or total_sales < MIN_HISTORY_SALES:
        return "abstain", (
            f"LAST-RESORT ESTIMATE — this product has neither enough of its own sales history "
            f"({days_history} days, {total_sales} units) nor enough similar products to borrow from, "
            f"so there is nothing solid to forecast from. It falls back to a flat moving average; "
            f"treat it as a rough placeholder, not a real forecast, until it builds up more history "
            f"or gets categorized alongside similar products.")

    # 6) Default: regular enough to use Prophet even if not 'established'.
    return "prophet", (
        f"This product has a regular sales pattern and enough history ({days_history} days) "
        f"to forecast directly with Prophet.")


ROUTE_LABELS = {
    "prophet":  "Prophet (own history)",
    "global":   "Global model (pooled from related products)",
    "croston":  "Croston/TSB (intermittent demand)",
    "abstain":  "Last-resort estimate — no history, no relatives",
}
