"""
sources.py — the registry that makes a live store connection pluggable.

WHY A REGISTRY RATHER THAN A SECOND `if`

shopify_source.py calls itself "a DECOUPLED data source", and for ingestion it genuinely
is: `_ingest` takes any DataFrame. But Shopify was still named explicitly in four other
places — the grading loop, the inventory poll, the two /api/livelog endpoints that call
them, and the cost-provenance string the frontend compares against. Adding Square as a
second branch in each would mean a third branch next time, and — the real hazard — a
livelog tick that quietly polls the wrong store because only three of the four branches
got updated.

So a source is a record with a fixed set of operations, and everything else looks them up
by key. Adding a platform means adding one entry here plus its fetch module. Nothing in
main.py learns its name.

NOTHING ABOUT THE SHOPIFY PATH CHANGES. The adapters below call the same functions with
the same arguments they were always called with; this is a move, not a rewrite.

CREDENTIAL FIELD SPECS

Each source declares its own `fields`, and the dashboard renders the form from that list
rather than hardcoding one form per platform. A new source gets a working credentials UI
for free, and the "saved accounts" picker works the same way for all of them.
"""
from __future__ import annotations

import live_actuals as _LA


# ─── Shopify ─────────────────────────────────────────────────────────────────

def _shopify_fetch_sales(creds, days=None):
    import shopify_source as SH
    return SH.fetch_sales(shop=creds.get("shop"), token=creds.get("token"),
                          api_version=creds.get("apiVersion"),
                          days=days if days is not None else creds.get("days"))


def _shopify_fetch_meta(creds):
    import shopify_source as SH
    return SH.fetch_catalog_meta(shop=creds.get("shop"), token=creds.get("token"),
                                 api_version=creds.get("apiVersion"))


def _shopify_label(creds):
    shop = (creds.get("shop") or "store").replace(".myshopify.com", "").strip()
    return f"Shopify · {shop}"


def _shopify_error(creds=None):
    import shopify_source as SH
    return SH.ShopifyError


# ─── Square ──────────────────────────────────────────────────────────────────

def _square_creds(creds):
    return {
        "access_token": creds.get("accessToken") or creds.get("access_token"),
        "environment": creds.get("environment") or "production",
        "api_version": creds.get("apiVersion"),
        "location_ids": creds.get("locationIds") or creds.get("location_ids"),
    }


def _square_fetch_sales(creds, days=None):
    import square_source as SQ
    c = _square_creds(creds)
    return SQ.fetch_sales(days=days if days is not None else creds.get("days"), **c)


def _square_fetch_today(creds):
    """Units sold SO FAR on the day in progress, per SKU: {sku: units}, plus the local day.

    Deliberately narrow. `days=2` because a store's local day can still be yesterday in
    UTC, so a one-day window can miss the very hours we are asking about. The frame comes
    back raw — unfiltered and unfilled — and is reduced to a dict here so no caller is
    ever handed something that could be mistaken for a training frame.
    """
    import square_source as SQ
    df = SQ.fetch_sales(days=2, include_today=True, **_square_creds(creds))
    day = str((df.attrs.get("square") or {}).get("localDay") or "")
    if not day or df is None or len(df) == 0:
        return {"day": day, "units": {}}
    today = df[df["date"].astype(str).str.slice(0, 10) == day]
    units = {str(k): float(v) for k, v in
             today.groupby("sku")["units_sold"].sum().items()}
    return {"day": day, "units": units}


def _shopify_fetch_today(creds):
    """Shopify has no partial-day path yet. Reported as unsupported rather than faked:
    an empty dict here would be indistinguishable from a genuine zero-sales day."""
    return {"day": None, "units": {}, "unsupported": True}


def _square_fetch_meta(creds):
    import square_source as SQ
    return SQ.fetch_catalog_meta(**_square_creds(creds))


def _square_label(creds):
    env = (creds.get("environment") or "production").lower()
    suffix = " (sandbox)" if env == "sandbox" else ""
    return f"Square{suffix}"


def _square_error(creds=None):
    import square_source as SQ
    return SQ.SquareError


# ─── The registry ────────────────────────────────────────────────────────────

SOURCES = {
    "shopify": {
        "key": "shopify",
        "label": "Shopify",
        "fetch_sales": _shopify_fetch_sales,
        "fetch_today": _shopify_fetch_today,
        "fetch_catalog_meta": _shopify_fetch_meta,
        "error_class": _shopify_error,
        "describe": _shopify_label,
        "fields": [
            {"name": "shop", "label": "Store name", "type": "text", "required": True,
             "placeholder": "store-name (before .myshopify.com)"},
            {"name": "token", "label": "Admin API token", "type": "password", "required": True,
             "secret": True, "placeholder": "Admin API access token",
             "hint": "Shopify admin → Settings → Apps → your app → API credentials"},
        ],
        "help": ("Use a read-only Admin API token (scopes read_orders, read_products). "
                 "Shopify returns only the last 60 days of orders unless the app has "
                 "read_all_orders approved, which a meaningful backtest needs."),
    },
    "square": {
        "key": "square",
        "label": "Square",
        "fetch_sales": _square_fetch_sales,
        "fetch_today": _square_fetch_today,
        "fetch_catalog_meta": _square_fetch_meta,
        "error_class": _square_error,
        "describe": _square_label,
        "fields": [
            {"name": "accessToken", "label": "Access token", "type": "password", "required": True,
             "secret": True, "placeholder": "Access token (not the Application ID)",
             "hint": "Square dashboard → your app → Credentials → Access token → Show"},
            {"name": "environment", "label": "Environment", "type": "select", "required": False,
             "options": [{"value": "production", "label": "Production"},
                         {"value": "sandbox", "label": "Sandbox"}],
             "default": "production"},
            {"name": "locationIds", "label": "Locations", "type": "locations", "required": False,
             "placeholder": "All active locations"},
        ],
        "help": ("Use the Access token from Square's Credentials page, not the Application ID "
                 "(that's only for OAuth). Scopes: ORDERS_READ, ITEMS_READ, INVENTORY_READ, "
                 "MERCHANT_PROFILE_READ. Square returns full order history with no approval. "
                 "Per-unit costs need Square for Retail Premium; without it you enter costs by hand."),
    },
}


def _sample_stock(slog, creds, source_key):
    """Take one inventory reading and file it — for any source.

    stock_log.sample_from_shopify is named for Shopify but, like live_actuals, is already
    generic: it takes a `fetch` callable and only imports Shopify when given none. So both
    platforms reuse its behaviour rather than reimplementing it — in particular the part
    that matters on an unattended hourly tick, which is that a store hiccup is swallowed
    into the return value instead of raised. A missed reading costs resolution on one day;
    an exception out of here would take forecast scoring down with it.
    """
    import stock_log as SL

    seen = {}

    def _fetch(**_ignored):
        seen["meta"] = SOURCES[source_key]["fetch_catalog_meta"](creds)
        return seen["meta"]

    res = SL.sample_from_shopify(slog, fetch=_fetch)
    # The same catalogue call carries the shelf price; hand it back rather than asking
    # the store a second time. See live_prices.py.
    meta = seen.get("meta") or {}
    if isinstance(res, dict) and meta:
        keys = ("listPrice", "currentPrice", "discountName", "discountFrom", "discountUntil")
        prices = {sku: {k: m.get(k) for k in keys} for sku, m in meta.items()
                  if isinstance(m, dict) and (m.get("listPrice") or m.get("currentPrice"))}
        if prices:
            res["prices"] = prices
    return res


def get(source_key: str) -> dict:
    src = SOURCES.get((source_key or "").strip().lower())
    if not src:
        raise KeyError(f"Unknown source {source_key!r}. Known: {', '.join(sorted(SOURCES))}")
    return src


def catalogue() -> list:
    """Browser-safe description of every source, for rendering the connect form."""
    return [{"key": s["key"], "label": s["label"], "fields": s["fields"], "help": s["help"]}
            for s in SOURCES.values()]


def fetch_sales(source_key: str, creds: dict, days=None):
    return get(source_key)["fetch_sales"](creds, days=days)


def fetch_today(source_key: str, creds: dict) -> dict:
    """{"day": "YYYY-MM-DD", "units": {sku: units}} for the day in progress.

    A source that cannot do this says so with `unsupported` rather than returning an
    empty dict, because "no partial-day support" and "nothing sold yet" must not look
    the same to a caller deciding whether to trust a stock level.
    """
    src = get(source_key)
    fn = src.get("fetch_today")
    if fn is None:
        return {"day": None, "units": {}, "unsupported": True}
    return fn(creds)


def fetch_catalog_meta(source_key: str, creds: dict):
    return get(source_key)["fetch_catalog_meta"](creds)


def sample_stock(source_key: str, slog, creds: dict):
    get(source_key)                                   # validate the key before using it
    return _sample_stock(slog, creds, (source_key or "").strip().lower())


def describe(source_key: str, creds: dict) -> str:
    return get(source_key)["describe"](creds)


def error_class(source_key: str):
    return get(source_key)["error_class"]()


def actuals_provider(source_key: str, entries, creds: dict, now=None):
    """Build the actuals_fn that ForecastLog.score_due wants, for any source.

    live_actuals.shopify_actuals_provider is named for Shopify but is already generic: it
    takes a `fetch` callable and only falls back to importing Shopify when none is given.
    So both platforms go through the same batching, the same one-query-covers-every-pending
    -window logic, and the same critical distinction between "sold zero" and "couldn't
    read the store". Passing an adapter here reuses all of that rather than copying it.
    """
    src = get(source_key)

    def _fetch(days=None, **_ignored):
        return src["fetch_sales"](creds, days=days)

    return _LA.shopify_actuals_provider(entries, now=now, fetch=_fetch)
