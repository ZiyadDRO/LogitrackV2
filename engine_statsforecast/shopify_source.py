"""
shopify_source.py — pull real sales from a Shopify store into the standard
dataframe the rest of the tool uses (Date, SKU, SKU_Name, Units_Sold, Price).

This is a DECOUPLED data source: it only fetches + reshapes. The backtest (and
the upload pipeline) don't know or care that the data came from Shopify — they
just receive the same columns they'd get from an Excel sheet.

ALL calls use the GraphQL Admin API. The original build used REST orders.json,
but Shopify made custom apps GraphQL-ONLY for partner organizations created
after April 1, 2025 — on those stores every REST endpoint returns
404 {"errors":"Not Found"}, which is exactly what a fresh dev store produces.
GraphQL works on every store, old or new.

⚠️  Logic is verified offline against fixture responses (test_shopify_source.py)
    but has NOT yet run against a live store. Validate once with a read-only
    Admin API token on a dev store before trusting its output.

Setup (read-only is enough — scopes: read_orders, read_products; add
read_all_orders to pull more than the last 60 days):
    export SHOPIFY_SHOP="your-store"            # the part before .myshopify.com
    export SHOPIFY_TOKEN="shpat_xxx"            # Admin API access token
    export SHOPIFY_API_VERSION="2025-10"        # optional
    export SHOPIFY_DAYS="730"                   # optional: how far back to pull

Then:
    python backtest.py --shopify
"""
from __future__ import annotations
import os
import time
import datetime
import pandas as pd

from forecast_engine import daily_price_rollup, carry_listed_price

try:
    import requests
except ImportError:                      # pragma: no cover
    requests = None


class ShopifyError(Exception):
    pass


def _cfg(shop=None, token=None, api_version=None, days=None):
    """Use explicit args (from the UI) when given, else fall back to env vars (CLI)."""
    shop = shop or os.environ.get("SHOPIFY_SHOP")
    token = token or os.environ.get("SHOPIFY_TOKEN")
    if not shop or not token:
        raise ShopifyError("Shopify shop name and access token are required.")
    shop = shop.replace(".myshopify.com", "").strip()
    ver = api_version or os.environ.get("SHOPIFY_API_VERSION", "2025-10")
    # days = None means "all history" (no date filter) — pull from the first order Shopify
    # will return (everything with read_all_orders, otherwise the last 60 days).
    raw_days = days if days not in (None, "") else os.environ.get("SHOPIFY_DAYS")
    try:
        days = int(raw_days) if raw_days not in (None, "", "0", 0, "all") else None
    except (TypeError, ValueError):
        days = None
    return shop, token, ver, days


def _graphql(url, headers, query, variables, retries=6):
    """POST one GraphQL request with retries. Handles both rate-limit shapes:
    HTTP 429 (Retry-After header) and HTTP 200 with a THROTTLED error body."""
    for _ in range(retries):
        resp = requests.post(url, json={"query": query, "variables": variables},
                             headers=headers, timeout=30)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code == 404:
            raise ShopifyError(
                "Shopify returned 404 Not Found, so the store name is almost certainly wrong. "
                "Enter only the part before .myshopify.com (e.g. 'my-store' for "
                "my-store.myshopify.com), not the display name or a custom domain.")
        if resp.status_code in (401, 403):
            raise ShopifyError(
                f"Shopify rejected the token (HTTP {resp.status_code}). Check the Admin API access "
                "token (shpat_…) and that the custom app is installed with read_orders and read_products scopes.")
        if resp.status_code != 200:
            raise ShopifyError(f"Shopify API error {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        errs = body.get("errors")
        if errs:
            throttled = any("THROTTLED" in str((e.get("extensions") or {}).get("code", ""))
                            for e in errs if isinstance(e, dict))
            if throttled:
                time.sleep(2.0)
                continue
            raise ShopifyError(f"Shopify GraphQL: {str(errs)[:200]}")
        return body
    raise ShopifyError("Shopify is rate-limiting this token and it didn't clear after "
                       "several retries. Wait a minute and try again.")


def _money(node, *path):
    """Follow a nested MoneyBag path and return a float, or None if absent."""
    try:
        for k in path:
            node = node[k]
        return float(node)
    except (KeyError, TypeError, ValueError):
        return None


# Page sizes are chosen to stay under Shopify's 1,000-point single-query cost cap
# (cost ≈ orders × (1 + line items) = 30 × 26 = 780). Orders with more than
# 25 line items have the remainder truncated — vanishingly rare in retail.
ORDERS_PER_PAGE = 30
ITEMS_PER_ORDER = 25

_ORDERS_QUERY = f"""
query($cursor: String, $q: String) {{
  shop {{ ianaTimezone }}
  orders(first: {ORDERS_PER_PAGE}, after: $cursor, query: $q) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      createdAt
      cancelledAt
      lineItems(first: {ITEMS_PER_ORDER}) {{
        nodes {{
          sku
          title
          quantity
          originalUnitPriceSet {{ shopMoney {{ amount }} }}
          totalDiscountSet {{ shopMoney {{ amount }} }}
          discountAllocations {{ allocatedAmountSet {{ shopMoney {{ amount }} }} }}
        }}
      }}
    }}
  }}
}}"""


def fetch_sales(shop=None, token=None, api_version=None, days=None) -> pd.DataFrame:
    """Return daily units sold per SKU: columns [date, sku, sku_name, units_sold, price,
    on_promotion]. Paginates through all orders in the lookback window (GraphQL cursor
    pagination) and aggregates line items to the day. Refunds are not yet subtracted
    (a refined version would net them out)."""
    if requests is None:
        raise ShopifyError("The 'requests' package is required: pip install requests")
    shop, token, ver, days = _cfg(shop, token, api_version, days)
    url = f"https://{shop}.myshopify.com/admin/api/{ver}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    q = None
    if days:                                  # omit the filter entirely → pull all history
        since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        q = f"created_at:>={since}"

    records, cursor = [], None
    # The shop's own time zone, read in the same request as the orders (a separate call
    # would cost a request for one field). Orders are timestamped in UTC, so taking the
    # first 10 characters filed every order after 7-8pm US time under the NEXT day:
    # weekday patterns skewed, and a Christmas Eve evening rush landed on Christmas Day.
    shop_tz = None
    while True:
        body = _graphql(url, headers, _ORDERS_QUERY, {"cursor": cursor, "q": q})
        if shop_tz is None:
            shop_tz = _zone_or_none(((body.get("data") or {}).get("shop") or {}).get("ianaTimezone"))
        conn = (body.get("data") or {}).get("orders") or {}
        for order in conn.get("nodes", []):
            # Cancelled orders never completed — that's not demand. Refunds (returns) are
            # left in: the demand happened and the returned unit goes back to sellable stock.
            if order.get("cancelledAt"):
                continue
            day = _local_day(order.get("createdAt"), shop_tz)
            for li in (order.get("lineItems") or {}).get("nodes", []):
                sku = li.get("sku") or li.get("title") or "UNKNOWN"
                qty = li.get("quantity") or 0
                # originalUnitPrice is the LISTED price; discount codes / automatic
                # discounts live in discountAllocations (Shopify's recommended source,
                # falling back to totalDiscountSet). Net them out to the real PAID price
                # and flag the day as on-promotion, so the model sees a genuine price
                # dip instead of a mystery demand spike.
                listed = _money(li, "originalUnitPriceSet", "shopMoney", "amount")
                disc = sum(filter(None, (_money(a, "allocatedAmountSet", "shopMoney", "amount")
                                         for a in (li.get("discountAllocations") or []))))
                if not disc:
                    disc = _money(li, "totalDiscountSet", "shopMoney", "amount") or 0.0
                paid = (listed - disc / qty) if (listed is not None and qty) else listed
                records.append({
                    "date": day, "sku": sku, "sku_name": li.get("title") or sku,
                    "units_sold": qty,
                    "price": round(paid, 2) if paid is not None else None,
                    "on_promotion": 1 if disc > 0 else 0,
                })
        info = conn.get("pageInfo") or {}
        if info.get("hasNextPage"):
            cursor = info.get("endCursor")
        else:
            break

    if not records:
        raise ShopifyError("No orders returned from Shopify for the requested window.")
    df = pd.DataFrame(records)
    # Units-weighted day price plus a mixed-day flag, instead of whichever line rang up
    # last. See forecast_engine.daily_price_rollup.
    df = daily_price_rollup(df)

    # THE WINDOW ENDS AT THE LAST COMPLETE DAY — NOT TODAY. See the long note in
    # square_source.fetch_sales: filling through today asserts a full day of no sales for
    # a day that is still in progress, and the nightly sync runs minutes after midnight.
    # "Yesterday" in the SHOP's time zone, not the server's.
    _last_complete = _today_in(shop_tz) - datetime.timedelta(days=1)
    df = df[pd.to_datetime(df["date"]).dt.date <= _last_complete]
    if df.empty:
        raise ShopifyError(
            "Shopify returned orders, but none from a completed day yet.")
    out = _fill_zero_days(df, window_end=_last_complete)
    out.attrs["shopify"] = {"timezone": shop_tz}
    return out


def _zone_or_none(name):
    """A valid IANA zone name, or None."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(str(name))
        return str(name)
    except Exception:                                   # noqa: BLE001
        return None


def _today_in(tz_name):
    """Today in the shop's zone; without one, the machine's local date, as before."""
    if tz_name:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    return datetime.datetime.now().astimezone().date()


def _local_day(created_at, tz_name) -> str:
    """The shop's calendar day an order belongs to. createdAt is ISO 8601 in UTC
    ("2026-12-25T01:30:00Z" is Christmas Eve evening in New York). Without a known zone
    the UTC day is kept, which is what this did before."""
    raw = str(created_at or "")
    if not raw:
        return ""
    if not tz_name:
        return raw[:10]
    try:
        from zoneinfo import ZoneInfo
        t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return t.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except (ValueError, TypeError):
        return raw[:10]


def _fill_zero_days(df: pd.DataFrame, window_end=None) -> pd.DataFrame:
    """Fill in the days that had no sale.

    Shopify only returns line items for days that HAD an order, so every day with no sale
    is simply ABSENT. Handing that to the engine averages demand over sale-days only — it
    never sees the zeros — so it badly OVER-forecasts (a product selling on 1 day in 7
    looks like it sells every day) and the demand classifier can't detect intermittency
    (ADI ~ 1 always). An uploaded sheet has a row per calendar day; this makes Shopify
    match: 0 units, not on promotion, and the shelf price carried forward (the listed
    price persisted on days nothing happened to sell).

    EVERY SERIES ENDS AT `window_end`, NOT AT ITS OWN LAST SALE — see the long note in
    square_source._fill_zero_days for what ending at the last sale cost: a series could
    never end in a zero, so `trailing_zero_run` was structurally always 0, `is_dormant`
    could never fire, and a discontinued product was forecast at its old rate forever.

    `window_end` is a parameter rather than an implicit "now" so this is testable against
    fixed dates; fetch_sales passes the real window end.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    end = pd.to_datetime(window_end).normalize() if window_end is not None else None
    filled = []
    for (sku, sku_name), g in df.groupby(["sku", "sku_name"]):
        g = g.set_index("date").sort_index()
        stop = g.index.max() if end is None else max(g.index.max(), end)
        full = pd.date_range(g.index.min(), stop, freq="D")
        g = g.reindex(full)
        g["units_sold"]   = g["units_sold"].fillna(0)
        g["on_promotion"] = g["on_promotion"].fillna(0)
        g = carry_listed_price(g)       # last SINGLE price, never a mixed day's blend
        g["sku"], g["sku_name"] = sku, sku_name
        g.index.name = "date"
        filled.append(g.reset_index())
    out = pd.concat(filled, ignore_index=True)
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out[["date", "sku", "sku_name", "units_sold", "price", "price_mixed", "on_promotion"]]


def _dec(x):
    """Shopify Money scalars arrive as strings ("24.99"), older versions as objects."""
    if isinstance(x, dict):
        x = x.get("amount")
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


# Shopify refuses any single query whose ESTIMATED cost is over 1,000 points, before
# running it. The estimate multiplies down the tree: a connection costs 2 plus `first`
# times what each node costs. products(first: 100) × variants(first: 100) came to about
# 30,000 points, so the catalogue read failed on every store (and with it the stock,
# cost and live-price prefill). Pages are sized to stay well under the limit, and a
# product with more variants than one page holds is finished with its own query.
MAX_QUERY_COST = 1000
PRODUCTS_PAGE = 8
VARIANTS_PAGE = 20
MORE_VARIANTS_PAGE = 100

_VARIANT_FIELDS = """
              sku
              inventoryQuantity
              price
              compareAtPrice
              inventoryItem { unitCost { amount } }"""

CATALOG_QUERY = f"""
    query($cursor: String) {{
      products(first: {PRODUCTS_PAGE}, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          productType
          variants(first: {VARIANTS_PAGE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{{_VARIANT_FIELDS}
            }}
          }}
        }}
      }}
    }}"""

MORE_VARIANTS_QUERY = f"""
    query($id: ID!, $cursor: String) {{
      product(id: $id) {{
        variants(first: {MORE_VARIANTS_PAGE}, after: $cursor) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{{_VARIANT_FIELDS}
          }}
        }}
      }}
    }}"""


def estimate_query_cost(query: str) -> int:
    """Shopify's requested-cost estimate for a query, the way its docs and engineering
    notes describe it: scalars and enums 0, every object 1, and a connection 2 plus
    `first` times the cost of one node (pageInfo counted once, not per node). Lists of
    objects are counted as one object, so this can only err low on those, never high on
    connections.

    A small parser over the selection set, good enough for the queries in this module;
    the test runs the catalogue queries through it so they can't drift over the limit."""
    import re
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\{|\}|\([^)]*\)", query)
    pos = 0

    def fields():
        """[(name, args, cost)] for one selection set; `cost` includes the field itself."""
        nonlocal pos
        out = []
        while pos < len(toks) and toks[pos] != "}":
            name = toks[pos]; pos += 1
            args = ""
            if pos < len(toks) and toks[pos].startswith("("):
                args = toks[pos]; pos += 1
            if pos < len(toks) and toks[pos] == "{":
                pos += 1
                kids = fields()
                pos += 1                                  # the closing brace
                m = re.search(r"\b(?:first|last)\s*:\s*(\d+)", args)
                if m:                                     # a connection
                    per_node = sum(c for n, _, c in kids if n in ("nodes", "edges"))
                    rest = sum(c for n, _, c in kids if n not in ("nodes", "edges"))
                    out.append((name, args, 2 + int(m.group(1)) * max(per_node, 1) + rest))
                elif name == "edges":
                    out.append((name, args, sum(c for _, _, c in kids)))
                else:                                     # an object (or the node list)
                    out.append((name, args, 1 + sum(c for _, _, c in kids)))
            else:
                out.append((name, args, 0))               # a scalar
        return out

    while pos < len(toks) and toks[pos] != "{":           # skip "query(...)"
        pos += 1
    pos += 1
    return sum(c for _, _, c in fields())


def _variant_meta(meta: dict, cat, v: dict) -> None:
    sku = v.get("sku")
    if not sku:
        return
    meta[sku] = {"category": cat, "stock": v.get("inventoryQuantity"),
                 "cost": _money(v, "inventoryItem", "unitCost", "amount")}
    # Shopify's own sale mechanism: `price` is what the customer pays and
    # `compareAtPrice`, when higher, is the original the storefront shows
    # struck through. No end date — Shopify doesn't keep one on the
    # variant — so a sale is treated as running until a sync sees it stop.
    price = _dec(v.get("price"))
    compare = _dec(v.get("compareAtPrice"))
    if price is not None and price > 0:
        listed = compare if (compare is not None and compare > price) else price
        meta[sku].update({"listPrice": round(listed, 2),
                          "currentPrice": round(price, 2)})
        if listed > price:
            meta[sku]["discountName"] = "Sale (compare-at price)"


def fetch_catalog_meta(shop=None, token=None, api_version=None) -> dict:
    """Per-SKU category (productType), CURRENT inventory, and per-unit COST via the
    GraphQL Admin API. Returns {sku: {"category": str|None, "stock": int|None,
    "cost": float|None}}. Needs scope read_products (cost also needs read_inventory
    on some stores — it degrades to None rather than failing).
    Note: inventoryQuantity is a CURRENT snapshot — Shopify exposes no historical daily
    stock, so this prefills the dashboard's stock field, not a backtest stock series."""
    if requests is None:
        raise ShopifyError("The 'requests' package is required: pip install requests")
    shop, token, ver, _ = _cfg(shop, token, api_version, None)
    url = f"https://{shop}.myshopify.com/admin/api/{ver}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    meta, cursor = {}, None
    while True:
        body = _graphql(url, headers, CATALOG_QUERY, {"cursor": cursor})
        conn = (body.get("data") or {}).get("products") or {}
        for p in conn.get("nodes", []):
            cat = p.get("productType") or None
            variants = p.get("variants") or {}
            for v in variants.get("nodes", []):
                _variant_meta(meta, cat, v)
            # A product with more variants than one page: finish it on its own.
            vinfo = variants.get("pageInfo") or {}
            vcursor = vinfo.get("endCursor")
            while vinfo.get("hasNextPage") and p.get("id") and vcursor:
                more = _graphql(url, headers, MORE_VARIANTS_QUERY, {"id": p["id"], "cursor": vcursor})
                mv = (((more.get("data") or {}).get("product") or {}).get("variants")) or {}
                for v in mv.get("nodes", []):
                    _variant_meta(meta, cat, v)
                vinfo = mv.get("pageInfo") or {}
                vcursor = vinfo.get("endCursor")
        info = conn.get("pageInfo") or {}
        if info.get("hasNextPage"):
            cursor = info.get("endCursor")
        else:
            break
    return meta


if __name__ == "__main__":
    out = fetch_sales()
    print(out.head(20).to_string(index=False))
    print(f"\n{len(out):,} daily rows across {out['sku'].nunique()} SKUs.")
