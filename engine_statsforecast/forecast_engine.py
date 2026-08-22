"""
forecast_engine.py — shared, model-agnostic helpers used by the router build.

This is now a small helper library (no heavy forecasting deps). The actual
forecasting lives in engines.py; which engine runs is decided by router.py and
orchestrated in main.py. Kept here:

  • analyse_price / price tiers / safe-extrapolation range  (carry over unchanged)
  • classify_demand (Syntetos–Boylan)                        (drives routing)
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# Three-tier price sampling thresholds.
MIN_DAYS_PRICE_INSUFFICIENT = 30
MIN_DAYS_PRICE_MODERATE     = 60
MIN_DAYS_PRICE_RELIABLE     = 90


# ════════════════════════════════════════════════════════════════════════════
#  DEMAND CLASSIFICATION  (Syntetos–Boylan)
# ════════════════════════════════════════════════════════════════════════════
def classify_demand(y: np.ndarray) -> str:
    """smooth | erratic | intermittent | lumpy | no_demand."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    nz = y[y > 0]
    if n == 0 or nz.size == 0:
        return "no_demand"
    adi = n / int(np.count_nonzero(y > 0))
    cv2 = (nz.std() / nz.mean()) ** 2 if nz.mean() > 0 else 0.0
    if adi < 1.32 and cv2 < 0.49:
        return "smooth"
    if adi < 1.32 and cv2 >= 0.49:
        return "erratic"
    if adi >= 1.32 and cv2 < 0.49:
        return "intermittent"
    return "lumpy"


def is_sparse(demand_class: str) -> bool:
    return demand_class in ("intermittent", "lumpy", "no_demand")


# A product selling on nearly half its days at a steady couple of units is a "slow
# recurring seller", not a rare-event product — even though the ADI test files both under
# "intermittent". The distinction matters for how you TALK about it: long gaps and rare
# sales deserve "may not sell for weeks"; a regular trickle deserves "about N a month".
# A silence this long, AND this far past the product's own typical gap, means stopped
# rather than quiet. Both conditions are required.
DORMANT_MIN_DAYS = 45
DORMANT_GAP_MULTIPLE = 4.0

LOW_VOLUME_REGULAR_MIN_SHARE = 0.30    # sells on ≥30% of days
LOW_VOLUME_REGULAR_MAX_RATE = 1.5      # but averages under this many units/day


def trailing_zero_run(y: np.ndarray) -> int:
    """How many days since the last sale."""
    y = np.asarray(y, dtype=float)
    n = 0
    for v in y[::-1]:
        if v > 0:
            break
        n += 1
    return n


def is_dormant(y: np.ndarray, *, min_days: int = DORMANT_MIN_DAYS,
               gap_multiple: float = DORMANT_GAP_MULTIPLE) -> bool:
    """Has this product simply stopped selling?

    Judged against its OWN rhythm, not a fixed threshold: 40 quiet days is nothing for a
    product that sells once a month and is a clear stop for one that sells every other
    day. Both a long absolute silence and a silence far beyond its usual gap are required,
    so an ordinary lull can't trip it.
    """
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return False
    tail = trailing_zero_run(y)
    if tail < min_days:
        return False
    nz = int((y > 0).sum())
    if nz == 0:
        return True
    adi = y.size / nz                      # average days between demands
    return tail >= max(min_days, adi * gap_multiple)


def sparse_subtype(y: np.ndarray, demand_class: str) -> str:
    """'low_volume_regular' | 'true_intermittent' | '' (not sparse)."""
    if not is_sparse(demand_class):
        return ""
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return "true_intermittent"
    # A product that sold steadily for eighteen months and then stopped still LOOKS
    # regular when you average the whole history — share and rate are both computed
    # over all of it. Calling it "regular" then excludes the one model that would decay
    # it, so a discontinued line goes on being forecast at its old rate indefinitely.
    if is_dormant(y):
        return "true_intermittent"
    share = float((y > 0).mean())
    rate = float(y.mean())
    if share >= LOW_VOLUME_REGULAR_MIN_SHARE and rate <= LOW_VOLUME_REGULAR_MAX_RATE:
        return "low_volume_regular"
    return "true_intermittent"


# ════════════════════════════════════════════════════════════════════════════
#  DAILY-CALENDAR GAP FILL
# ════════════════════════════════════════════════════════════════════════════
def fill_daily_gaps(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Reindex ONE SKU's frame (must have a 'ds' datetime column) to a continuous
    daily calendar from its first to its last row, filling the gaps.

    Why: many exports only write a row for days that HAD a sale. Handing that
    straight to the engines averages demand over sale-days only — the model never
    sees the zeros — so it over-forecasts, and the demand classifier can't detect
    intermittency (ADI ≈ 1 always). The Shopify source already does this fill;
    this makes file uploads (and the backtest) behave identically.

    Fills: y → 0, on_promotion → 0, price → ffill/bfill, units_in_stock →
    ffill/bfill then 1 (assume in stock when unknown — matches upload hygiene),
    any other column (attributes like category) → ffill/bfill (per-SKU constants).
    Returns (filled_df, days_added)."""
    if df is None or len(df) < 2 or "ds" not in df.columns:
        return df, 0
    d = df.sort_values("ds").copy()
    d["ds"] = pd.to_datetime(d["ds"])
    full = pd.date_range(d["ds"].min(), d["ds"].max(), freq="D")
    if len(full) <= len(d):
        return d.reset_index(drop=True), 0
    n_added = int(len(full) - len(d))
    d = d.set_index("ds").reindex(full)
    d.index.name = "ds"
    for c in d.columns:
        if c == "y":
            d[c] = d[c].fillna(0)
        elif c == "on_promotion":
            d[c] = d[c].fillna(0)
        elif c == "price":
            d[c] = d[c].ffill().bfill()
        elif c == "units_in_stock":
            d[c] = d[c].ffill().bfill().fillna(1)
        else:
            d[c] = d[c].ffill().bfill()
    return d.reset_index(), n_added


# ════════════════════════════════════════════════════════════════════════════
#  PRICE ANALYSIS  (model-agnostic)
# ════════════════════════════════════════════════════════════════════════════
def _tier_quality(days: int) -> str:
    if days >= MIN_DAYS_PRICE_RELIABLE:
        return "reliable"
    if days >= MIN_DAYS_PRICE_MODERATE:
        return "moderate"
    if days >= MIN_DAYS_PRICE_INSUFFICIENT:
        return "weak"
    return "insufficient"


def analyse_price(df: pd.DataFrame):
    """(price_varied, price_well_sampled, last_price, price_tiers, warn_level, warn_msg)."""
    if "price" not in df.columns:
        return False, False, None, [], "no_column", (
            "No price data was uploaded for this SKU. Price changes cannot be factored into the "
            "forecast. Add a 'Price' column to your Excel sheet to enable price sensitivity.")
    prices = df["price"].dropna()
    if prices.empty:
        return False, False, None, [], "no_column", "Price column is present but contains no valid values."

    last_price = float(prices.iloc[-1])
    price_varied = prices.nunique() > 1
    if not price_varied:
        return False, False, last_price, [], "never_changed", None

    tiers = sorted(
        [{"price": float(p), "days": int(d), "quality": _tier_quality(int(d))}
         for p, d in prices.value_counts().items()],
        key=lambda t: t["price"])
    well = all(t["quality"] == "reliable" for t in tiers)
    if not well:
        nr = [t for t in tiers if t["quality"] != "reliable"]
        desc = ", ".join(f"${t['price']:.2f} ({t['days']}d — {t['quality']})" for t in nr)
        return True, False, last_price, tiers, "thin_data", (
            f"The model has seen price variation, but some price points are not yet reliably sampled "
            f"({desc}). Price sensitivity may be unreliable until each tier reaches 90+ days of data.")
    return True, True, last_price, tiers, None, None


def compute_safe_price_range(fit_df, price_tiers, p_min, p_max, residual_std, price_coef=None):
    """Statistically-grounded safe extrapolation range beyond observed prices.
    Model-agnostic: takes the backtest residual sigma (and, when available, an
    exogenous price coefficient); no model object required."""
    try:
        if not price_tiers or p_min is None or p_max is None:
            return None, None
        rng = p_max - p_min
        if rng < 0:
            return p_min, p_max
        sigma = float(residual_std)
        price_std = float(fit_df["price"].std()) if "price" in fit_df.columns else 0.0
        n = len(fit_df)
        if price_coef is not None and abs(price_coef) > 1e-9:
            stat = sigma / abs(price_coef)
        elif price_std > 0 and sigma > 0:
            stat = price_std * np.sqrt(n)
        else:
            stat = 0.0
        mean_price = float(fit_df["price"].mean()) if "price" in fit_df.columns else p_min
        max_ext = min(0.25 * rng if rng > 0 else 0.10 * mean_price, 0.10 * mean_price)
        lo_days = next((t["days"] for t in price_tiers if t["price"] == p_min), 30)
        hi_days = next((t["days"] for t in price_tiers if t["price"] == p_max), 30)
        ext_lo = max_ext * min(lo_days / MIN_DAYS_PRICE_RELIABLE, 1.0)
        ext_hi = max_ext * min(hi_days / MIN_DAYS_PRICE_RELIABLE, 1.0)
        if stat > 0:
            ext_lo = min(ext_lo, stat); ext_hi = min(ext_hi, stat)
        return round(max(0.0, p_min - ext_lo), 2), round(p_max + ext_hi, 2)
    except Exception as e:
        print(f"[safe_price_range] {e}")
        return p_min, p_max
