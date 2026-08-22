"""
Promotions — stopping Black Friday from becoming February's normal.

Prophet handles this properly and always has: `on_promotion` goes in as a regressor, so
the model learns how much a promo lifts sales and then forecasts ordinary days with the
regressor set to zero. The spike is measured and set aside, which is better than ignoring
it — you keep the information about how big the lift was.

The other three routes have no such input. `GlobalPooledEngine.fit(df, today, related)`,
`IntermittentEngine.fit(df, today)` and `MovingAverageEngine.fit(df, today)` see only
dates and quantities, so a promotional week is indistinguishable from a genuine step up
in demand. For those routes a Black Friday really would raise the February baseline.

That gap matters more than it sounds, because of WHICH products take those routes: new
products (pooled) and intermittent sellers (Croston). A launch promotion is exactly the
kind of thing a brand-new product has, and it lands on the route least able to cope.

The fix here is the blunt one, used only where the good one isn't available: hold promo
days out of the fit. Two guards make that safe rather than destructive —

  * enough ordinary days must remain to fit anything at all;
  * if promotions dominate the history, dropping them would leave a model of a business
    that doesn't exist, so nothing is dropped and the caller is told.
"""

from __future__ import annotations

# Below this many ordinary days, removing anything leaves too little to fit.
MIN_REMAINING_DAYS = 45

# Above this share, promotions ARE the business. A store that discounts most weeks would
# otherwise be modelled on its rare full-price days and would under-forecast badly.
MAX_PROMO_SHARE = 0.40

# Routes that model promotions themselves and must not be touched.
PROMO_AWARE_ROUTES = {"prophet"}


def promo_mask(df, col: str = "on_promotion"):
    if df is None or len(df) == 0 or col not in df.columns:
        return None
    return df[col].fillna(0).astype(float) >= 1


def hold_out(df, route: str, *, col: str = "on_promotion",
             min_remaining: int = MIN_REMAINING_DAYS,
             max_share: float = MAX_PROMO_SHARE):
    """Remove promotional days for routes that can't model them.

    Returns (frame, report). The frame is returned unchanged whenever holding days out
    would be wrong or unnecessary, and the report always says which of those it was — a
    silent no-op and a deliberate no-op look identical from the outside otherwise.
    """
    base = {"route": route, "applied": False, "promoDays": 0, "removed": 0,
            "remaining": 0 if df is None else len(df), "reason": None}

    if route in PROMO_AWARE_ROUTES:
        # Prophet subtracts the promo effect properly; stripping the days would destroy
        # the very evidence it uses to do that.
        return df, {**base, "reason": "route-models-promotions"}

    m = promo_mask(df, col)
    if m is None:
        return df, {**base, "reason": "no-promotion-data"}

    promo_days = int(m.sum())
    if promo_days == 0:
        return df, {**base, "reason": "no-promotions-in-history"}

    total = len(df)
    share = promo_days / total
    remaining = total - promo_days

    if share > max_share:
        return df, {**base, "promoDays": promo_days, "remaining": total,
                    "reason": "promotions-are-the-norm", "share": round(share, 3)}
    if remaining < min_remaining:
        return df, {**base, "promoDays": promo_days, "remaining": total,
                    "reason": "too-little-left", "share": round(share, 3)}

    kept = df[~m].copy()
    return kept, {**base, "applied": True, "promoDays": promo_days,
                  "removed": promo_days, "remaining": len(kept),
                  "share": round(share, 3), "reason": "held-out"}


def explain(report: dict) -> str:
    """One line for the product panel, only when there's something to say."""
    if not report:
        return ""
    r = report.get("reason")
    if r == "held-out":
        return (f"{report['removed']} promotional days were left out of the forecast so a "
                f"discount period doesn't become the new baseline. This product's model "
                f"can't separate promo demand on its own.")
    if r == "promotions-are-the-norm":
        return (f"{report['promoDays']} of {report['remaining']} days were promotional. "
                f"That's too many to exclude — discounting looks like part of how this "
                f"product normally sells, so the forecast includes it.")
    if r == "too-little-left":
        return (f"There are {report['promoDays']} promotional days, but removing them "
                f"would leave too little history to forecast from, so they're kept.")
    return ""
