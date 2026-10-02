"""
square_source.py — pull real sales from a Square seller account into the standard
dataframe the rest of the tool uses (date, sku, sku_name, units_sold, price, on_promotion).

Same contract as shopify_source.py, and for the same reason: this is a DECOUPLED data
source. It only fetches + reshapes. The backtest and the upload pipeline don't know or
care that the data came from Square — they receive the columns they'd get from an Excel
sheet.

ALL calls use the Square Connect v2 REST API (there is no GraphQL equivalent). Auth is a
bearer token: either a personal access token from the seller's own developer dashboard
(fastest for a single store) or an OAuth access token (what a multi-tenant product needs).

⚠️  Logic is verified offline against fixture responses (test_square_source.py) but has
    NOT yet run against a live Square account. Reconcile against Square's own sales
    reports before trusting its output — see SQUARE_AND_LOCATIONS.md.

FOUR THINGS SQUARE DOES DIFFERENTLY FROM SHOPIFY, ALL OF WHICH BITE

1. SKU IS NOT ON THE LINE ITEM. Shopify hands you `lineItems.nodes[].sku` directly.
   Square hands you `line_items[].catalog_object_id` — the ID of an ITEM_VARIATION — and
   the SKU lives on the catalog object. So the catalog is fetched FIRST and the sales are
   joined onto it. Two things then go wrong in real accounts, and both are counted and
   reported rather than silently swallowed:
     · line items with no catalog_object_id — an amount typed into the register
       ("Misc $8.00"). Real revenue, not a trackable product. Dropped.
     · variations with an empty `sku` — Square does not require one and plenty of
       merchants never fill it in. Falls back to the catalog_object_id, which is stable
       and unique but opaque in the UI.

2. MONEY IS INTEGER CENTS. `{"amount": 1250}` is $12.50, where Shopify gives "12.50".
   Getting this wrong inflates every price by 100x, and the margin-based protection tiers
   would produce confident nonsense rather than an obvious error.

3. DATES MUST BE LOCALIZED PER LOCATION. Orders carry RFC3339 UTC timestamps. A 9pm
   Saturday sale in Georgia is SUNDAY in UTC. Everything downstream aggregates to a
   calendar day and reads day-of-week seasonality off it, so every order is converted to
   its own location's IANA timezone (from the Locations API) before being truncated to a
   day. Shopify let us ignore this because a store has one timezone; Square won't,
   because locations can sit in different ones.

4. QUANTITIES AND INVENTORY COUNTS ARE STRINGS. `"quantity": "2"`, not 2.

WHAT THIS MODULE DELIBERATELY DOES NOT DO YET

Multi-location. `fetch_sales` accepts several locations and pools them into one series
per SKU, which is what the current single-location catalog expects. That is the right
first step and the WRONG final answer — see SQUARE_AND_LOCATIONS.md Part 2 for why
pooling hides a per-store stockout. `location_id` is carried through on every row so the
split is a grouping change, not a re-fetch.

Setup (read-only is enough — scopes: ORDERS_READ, ITEMS_READ, INVENTORY_READ,
MERCHANT_PROFILE_READ).

NOTE ON WHICH CREDENTIAL: Square's Credentials page shows an Application ID AND an Access
token. This integration wants the ACCESS TOKEN. The Application ID identifies an app
during the OAuth handshake and is never sent here — `_check_token_shape` says so out loud
if one is pasted by mistake, because the alternative is an unexplained HTTP 401.

    export SQUARE_ACCESS_TOKEN="EAAA..."        # the Access token, NOT the Application ID
    export SQUARE_ENVIRONMENT="production"      # or "sandbox"
    export SQUARE_LOCATION_IDS="L1,L2"          # optional: defaults to every active location
    export SQUARE_DAYS="730"                    # optional: how far back to pull
"""
from __future__ import annotations

import datetime
import os
import random
import time
from zoneinfo import ZoneInfo

import pandas as pd

from forecast_engine import daily_price_rollup, carry_listed_price

try:
    import requests
except ImportError:                      # pragma: no cover
    requests = None


class SquareError(Exception):
    pass


PRODUCTION_BASE = "https://connect.squareup.com"
SANDBOX_BASE = "https://connect.squareupsandbox.com"

# Square caps SearchOrders at 10 location IDs per request, and BatchRetrieveInventoryCounts
# at 500 catalog object IDs. Two stores never approach either, but the product is sold to
# people who might have twelve, so both are chunked.
MAX_LOCATIONS_PER_QUERY = 10
MAX_CATALOG_IDS_PER_QUERY = 500
ORDERS_PAGE_LIMIT = 500                  # Square's default; max is 1000


# Square's Credentials page shows TWO values side by side, and only one of them is a
# credential you can call the API with. The Application ID identifies an app during the
# OAuth handshake and is not used anywhere in this integration; pasting it produces a bare
# HTTP 401, which reads as "my token is wrong" rather than "that is the wrong field".
# These prefixes are documented, so the mix-up can be named exactly instead of guessed at.
#   production application ID:  sq0idp-LJ1Sr4Iim0hGGvsMrx83vF
#   sandbox application ID:     sandbox-sq0idb-ioiyW39PwrXoGyLt4
_APP_ID_PREFIXES = ("sq0idp-", "sandbox-sq0idb-", "sq0idb-")


def _check_token_shape(token: str) -> None:
    """Catch the Application-ID-for-Access-token mix-up before spending a round trip on it."""
    t = token.strip()
    low = t.lower()
    if low.startswith(_APP_ID_PREFIXES) or low.startswith("sandbox-sq0id"):
        raise SquareError(
            "That looks like your Square Application ID, not your Access token. In the "
            "Square developer dashboard's Credentials page, copy the one labelled "
            "'Access token' (click Show to reveal it).")
    if low.startswith(("shpat_", "shpca_", "shppa_")):
        raise SquareError(
            "That's a Shopify Admin API token, not a Square one. Switch the source to "
            "Shopify, or paste the Square Access token instead.")


def _cfg(access_token=None, environment=None, api_version=None, days=None, location_ids=None):
    """Use explicit args (from the UI) when given, else fall back to env vars (CLI)."""
    token = access_token or os.environ.get("SQUARE_ACCESS_TOKEN")
    if not token:
        raise SquareError("A Square access token is required.")
    # Pasting from a dashboard drags whitespace along more often than not.
    token = str(token).strip()
    if not token:
        raise SquareError("A Square access token is required.")
    _check_token_shape(token)
    env = (environment or os.environ.get("SQUARE_ENVIRONMENT") or "production").strip().lower()
    if env not in ("production", "sandbox"):
        raise SquareError(f"Unknown Square environment {env!r}. Use 'production' or 'sandbox'.")
    # api_version is OPTIONAL ON PURPOSE. Omitting the Square-Version header tells Square to
    # use the version pinned to the application in its developer dashboard, which is always
    # valid. Hardcoding a date string here would break every call the day it goes stale, to
    # buy nothing — so it is only sent when someone deliberately sets one.
    ver = api_version or os.environ.get("SQUARE_API_VERSION") or None

    raw_days = days if days not in (None, "") else os.environ.get("SQUARE_DAYS")
    try:
        parsed_days = int(raw_days) if raw_days not in (None, "", "0", 0, "all") else None
    except (TypeError, ValueError):
        parsed_days = None

    locs = location_ids if location_ids not in (None, "", []) else os.environ.get("SQUARE_LOCATION_IDS")
    if isinstance(locs, str):
        locs = [p.strip() for p in locs.split(",") if p.strip()]
    return token, env, ver, parsed_days, (list(locs) if locs else None)


def _request(method, path, token, api_version=None, environment="production",
             json_body=None, params=None, retries=6, session=None):
    """One REST call with retries.

    Square answers a rate limit with HTTP 429 and does NOT document a Retry-After header,
    so this backs off exponentially with jitter rather than reading one — a fixed sleep
    from several workers is how you get a thundering herd.
    """
    if requests is None:
        raise SquareError("The 'requests' package is required: pip install requests")
    base = SANDBOX_BASE if environment == "sandbox" else PRODUCTION_BASE
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if api_version:
        headers["Square-Version"] = api_version
    http = session or requests

    delay = 1.0
    for attempt in range(retries):
        resp = http.request(method, url, json=json_body, params=params,
                            headers=headers, timeout=30)
        if resp.status_code == 429:
            time.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 20.0)
            continue
        if resp.status_code in (401, 403):
            raise SquareError(
                f"Square rejected the token (HTTP {resp.status_code}). Check the access token and "
                "that it has the read-only scopes ORDERS_READ, ITEMS_READ, INVENTORY_READ and "
                "MERCHANT_PROFILE_READ. A sandbox token won't work against production.")
        if resp.status_code == 404:
            raise SquareError(
                f"Square returned 404 for {path}. Check the environment "
                f"({'sandbox' if environment == 'sandbox' else 'production'}) matches the token.")
        if resp.status_code >= 400:
            raise SquareError(f"Square API error {resp.status_code}: {_errors_text(resp)}")
        try:
            body = resp.json()
        except ValueError:
            raise SquareError(f"Square returned a non-JSON response: {resp.text[:200]}")
        errs = body.get("errors")
        if errs:
            if any((e or {}).get("code") == "RATE_LIMITED" for e in errs if isinstance(e, dict)):
                time.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 20.0)
                continue
            raise SquareError(f"Square: {_errors_summary(errs)}")
        return body
    raise SquareError("Square is rate-limiting this token and it didn't clear after "
                      "several retries. Wait a minute and try again.")


def _errors_text(resp):
    try:
        return _errors_summary(resp.json().get("errors") or [])
    except ValueError:
        return resp.text[:200]


def _errors_summary(errs):
    if not errs:
        return "unknown error"
    parts = []
    for e in errs:
        if not isinstance(e, dict):
            continue
        bit = e.get("detail") or e.get("code") or "error"
        if e.get("field"):
            bit = f"{bit} (field: {e['field']})"
        parts.append(bit)
    return "; ".join(parts)[:300] or "unknown error"


def _money(node, *path):
    """Follow a nested Money path and return DOLLARS as a float, or None if absent.

    Square reports money as an integer count of the currency's smallest unit, so a USD
    `amount` of 1250 is $12.50.
    """
    try:
        for k in path:
            node = node[k]
        if node is None:
            return None
        return float(node) / 100.0
    except (KeyError, TypeError, ValueError):
        return None


def _num(value, default=0.0):
    """Square sends quantities as strings ('2', '47.5'). Parse defensively."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ─── Locations ───────────────────────────────────────────────────────────────

def fetch_locations(access_token=None, environment=None, api_version=None, session=None) -> list:
    """Every location on the account: [{id, name, timezone, currency, status, type}].

    The timezone is the part that matters downstream — see note 3 in the module docstring.
    """
    token, env, ver, _, _ = _cfg(access_token, environment, api_version)
    body = _request("GET", "/v2/locations", token, ver, env, session=session)
    out = []
    for loc in body.get("locations") or []:
        out.append({
            "id": loc.get("id"),
            "name": loc.get("name") or loc.get("id"),
            "timezone": loc.get("timezone") or "UTC",
            "currency": loc.get("currency"),
            "status": loc.get("status"),
            "type": loc.get("type"),
        })
    if not out:
        raise SquareError("This Square account has no locations visible to the token.")
    return out


def _active_location_ids(locations, wanted=None):
    """Resolve which locations to pull. Defaults to every ACTIVE location."""
    by_id = {l["id"]: l for l in locations}
    if wanted:
        missing = [w for w in wanted if w not in by_id]
        if missing:
            available = ", ".join("{} ({})".format(l["name"], l["id"]) for l in locations)
            raise SquareError(
                f"Location(s) not found on this Square account: {', '.join(missing)}. "
                f"Available: {available}")
        return list(wanted)
    active = [l["id"] for l in locations if (l.get("status") or "ACTIVE") == "ACTIVE"]
    return active or [l["id"] for l in locations]


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ─── Catalog ─────────────────────────────────────────────────────────────────

def fetch_catalog(access_token=None, environment=None, api_version=None, session=None,
                  pricing_out: dict | None = None) -> dict:
    """Map every item variation to the identity the rest of the tool uses.

    Returns {catalog_object_id: {sku, name, item_name, category, price}}.

    `sku` falls back to the catalog_object_id when the merchant left the SKU field blank,
    which is common. The fallback is stable and unique, so the forecast is correct — it
    just shows an opaque Square ID where a SKU should be. `fetch_sales` counts how often
    this happens so the import can say so out loud.
    """
    token, env, ver, _, _ = _cfg(access_token, environment, api_version)

    categories, variations = {}, {}
    # Automatic discounts, read in the same listing: a DISCOUNT says how much, a
    # PRODUCT_SET says on what, and a PRICING_RULE ties the two to a date window. Together
    # they are what Square's register applies without anyone keying a discount in — the
    # sale price a customer sees. Collected only when the caller asks (pricing_out).
    discounts, rules, product_sets = {}, {}, {}
    cursor = None
    while True:
        params = {"types": "ITEM,CATEGORY,DISCOUNT,PRICING_RULE,PRODUCT_SET"}
        if cursor:
            params["cursor"] = cursor
        body = _request("GET", "/v2/catalog/list", token, ver, env, params=params, session=session)
        for obj in body.get("objects") or []:
            otype = obj.get("type")
            if otype == "CATEGORY":
                categories[obj.get("id")] = (obj.get("category_data") or {}).get("name")
            elif otype == "DISCOUNT":
                discounts[obj.get("id")] = obj.get("discount_data") or {}
            elif otype == "PRICING_RULE":
                rules[obj.get("id")] = obj.get("pricing_rule_data") or {}
            elif otype == "PRODUCT_SET":
                product_sets[obj.get("id")] = obj.get("product_set_data") or {}
            elif otype == "ITEM":
                item = obj.get("item_data") or {}
                item_name = item.get("name") or obj.get("id")
                cat_id = _category_id(item)
                for var in item.get("variations") or []:
                    vid = var.get("id")
                    vd = var.get("item_variation_data") or {}
                    if not vid:
                        continue
                    var_name = vd.get("name")
                    overrides = {}
                    for o in vd.get("location_overrides") or []:
                        amt = _money(o, "price_money", "amount")
                        if o.get("location_id") and amt is not None:
                            overrides[o["location_id"]] = amt
                    variations[vid] = {
                        "item_id": obj.get("id"),
                        "category_ids": _all_category_ids(item),
                        "price_overrides": overrides,
                        "sku": (vd.get("sku") or "").strip() or vid,
                        "has_sku": bool((vd.get("sku") or "").strip()),
                        "name": f"{item_name} ({var_name})" if var_name and var_name != item_name else item_name,
                        "item_name": item_name,
                        "category_id": cat_id,
                        "price": _money(vd, "price_money", "amount"),
                        "cost": _vendor_cost(vd),
                    }
        cursor = body.get("cursor")
        if not cursor:
            break

    for v in variations.values():
        v["category"] = categories.get(v.pop("category_id"))
    if pricing_out is not None:
        pricing_out.update({"discounts": discounts, "rules": rules, "product_sets": product_sets})
    return variations


def _all_category_ids(item_data) -> list:
    ids = []
    rc = (item_data.get("reporting_category") or {}).get("id")
    if rc:
        ids.append(rc)
    for c in item_data.get("categories") or []:
        if isinstance(c, dict) and c.get("id"):
            ids.append(c["id"])
    if item_data.get("category_id"):
        ids.append(item_data["category_id"])
    return list(dict.fromkeys(ids))


def _variation_price(info, loc_ids):
    """The shelf price at the store: a location override wins over the base price.
    With several locations the first one that overrides decides; the single-location
    model this tool uses has only one."""
    for lid in loc_ids or []:
        if lid in (info.get("price_overrides") or {}):
            return info["price_overrides"][lid]
    return info.get("price")


def _set_matches(pset: dict, ids: set) -> bool:
    if not pset:
        return False
    if pset.get("all_products"):
        return True
    anyof = set(pset.get("product_ids_any") or [])
    allof = set(pset.get("product_ids_all") or [])
    if anyof and ids & anyof:
        return True
    # "product_ids_all" means the ORDER must contain all of them — a bundle deal. A
    # single product on the shelf is not on sale because of it, unless it is the only one.
    if allof and allof <= ids:
        return True
    return False


def active_discount(info: dict, pricing: dict, local_day: str, list_price):
    """The automatic discount a customer would get on this product today, or None.

    Only discounts that change the SHELF price count: a rule that needs a minimum order,
    a quantity, a customer group, a PIN, or a manager to key in an amount is not the
    price on display — it is a price some checkouts get. Rules with a recurring time
    window (happy hour) are skipped too; they are not in force all day. When several
    apply, the deepest wins, which is what Square's register does for automatic
    discounts on the same item.

    Returns {"price", "name", "from", "until"} or None.
    """
    if not pricing or list_price is None or list_price <= 0:
        return None
    ids = {info.get("id"), info.get("item_id"), *(info.get("category_ids") or [])} - {None}
    best = None
    for rule in (pricing.get("rules") or {}).values():
        if not rule.get("discount_id") or not rule.get("match_products_id"):
            continue
        if rule.get("time_period_ids") or rule.get("customer_group_ids_any"):
            continue
        if rule.get("minimum_order_subtotal_money") or rule.get("apply_products_id"):
            continue
        frm, until = rule.get("valid_from_date"), rule.get("valid_until_date")
        if frm and local_day < frm:
            continue
        if until and local_day > until:
            continue
        pset = (pricing.get("product_sets") or {}).get(rule["match_products_id"]) or {}
        if pset.get("quantity_min") or pset.get("quantity_exact") or pset.get("quantity_max"):
            continue
        if not _set_matches(pset, ids):
            continue
        ex = (pricing.get("product_sets") or {}).get(rule.get("exclude_products_id") or "")
        if ex and _set_matches(ex, ids) and not ex.get("all_products"):
            continue
        d = (pricing.get("discounts") or {}).get(rule["discount_id"]) or {}
        if d.get("pin_required"):
            continue
        dtype = d.get("discount_type")
        if dtype == "FIXED_PERCENTAGE":
            pct = _num(d.get("percentage"), None)
            if pct is None:
                continue
            price = list_price * (1 - pct / 100.0)
            cap = _money(d, "maximum_amount_money", "amount")
            if cap is not None:
                price = max(price, list_price - cap)
        elif dtype == "FIXED_AMOUNT":
            amt = _money(d, "amount_money", "amount")
            if amt is None:
                continue
            price = list_price - amt
        else:
            continue                         # VARIABLE_*: keyed in at the register
        price = round(max(price, 0.0), 2)
        if price >= list_price - 0.005:
            continue
        if best is None or price < best["price"]:
            best = {"price": price, "name": d.get("name") or rule.get("name"),
                    "from": frm, "until": until}
    return best


def _category_id(item_data):
    """Square has moved the category around across API versions. Try newest first."""
    rc = item_data.get("reporting_category") or {}
    if rc.get("id"):
        return rc["id"]
    cats = item_data.get("categories") or []
    if cats and isinstance(cats[0], dict) and cats[0].get("id"):
        return cats[0]["id"]
    return item_data.get("category_id")


def _vendor_cost(variation_data):
    """Per-unit cost, if this account exposes one.

    Square puts cost in vendor information on the item variation, and that is gated behind
    Square for Retail Premium / Restaurants Premium. On most accounts it simply is not
    there, so this degrades to None rather than failing — and the dashboard asks for costs
    by hand, exactly as it does for a spreadsheet with no Cost column. Shopify gave this
    away for free; Square usually will not.
    """
    for key in ("item_variation_vendor_infos", "vendor_infos", "vendor_information"):
        infos = variation_data.get(key)
        if not infos:
            continue
        if isinstance(infos, dict):
            infos = [infos]
        for info in infos:
            if not isinstance(info, dict):
                continue
            data = info.get("item_variation_vendor_info_data") or info
            cost = _money(data, "price_money", "amount")
            if cost is not None:
                return cost
    return None


# ─── Sales ───────────────────────────────────────────────────────────────────

_SKIP_LINE_ITEM_TYPES = {"GIFT_CARD", "CUSTOM_AMOUNT"}


def fetch_sales(access_token=None, environment=None, api_version=None, days=None,
                location_ids=None, session=None, catalog=None,
                include_today=False) -> pd.DataFrame:
    """Return daily units sold per SKU: columns [date, sku, sku_name, units_sold, price,
    on_promotion, location_id].

    Import statistics ride along on `df.attrs["square"]` so the endpoint can report what
    was dropped and why without a second call.
    """
    token, env, ver, lookback, wanted = _cfg(access_token, environment, api_version, days, location_ids)

    locations = fetch_locations(token, env, ver, session=session)
    loc_ids = _active_location_ids(locations, wanted)
    loc_tz = {l["id"]: l.get("timezone") or "UTC" for l in locations}
    loc_name = {l["id"]: l["name"] for l in locations}

    if catalog is None:
        catalog = fetch_catalog(token, env, ver, session=session)

    start_at = None
    if lookback:
        start_at = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=lookback)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    stats = {"orders": 0, "lineItems": 0, "adHocLineItems": 0, "unmappedCatalogObjects": 0,
             "skusWithoutSkuField": set(), "locations": [loc_name.get(i, i) for i in loc_ids],
             # The store's time zone for the app's clock (main.py store zone): the first
             # location pulled. Sales are already dated per location in its own zone.
             "timezone": next((loc_tz.get(i) for i in loc_ids if loc_tz.get(i) and loc_tz.get(i) != "UTC"), None)}
    records = []

    for chunk in _chunks(loc_ids, MAX_LOCATIONS_PER_QUERY):
        cursor = None
        while True:
            # closed_at + CLOSED_AT + a state_filter are a package deal: Square requires the
            # sort field to match the date filter, and requires a state filter whenever you
            # sort by CLOSED_AT. COMPLETED only — a CANCELED order never happened, which is
            # the same call shopify_source makes on cancelledAt.
            date_filter = {"closed_at": {"start_at": start_at}} if start_at else {}
            query = {
                "filter": {"state_filter": {"states": ["COMPLETED"]}},
                "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
            }
            if date_filter:
                query["filter"]["date_time_filter"] = date_filter
            payload = {"location_ids": chunk, "query": query, "limit": ORDERS_PAGE_LIMIT}
            if cursor:
                payload["cursor"] = cursor

            body = _request("POST", "/v2/orders/search", token, ver, env,
                            json_body=payload, session=session)
            for order in body.get("orders") or []:
                stats["orders"] += 1
                oloc = order.get("location_id")
                ts = order.get("closed_at") or order.get("created_at")
                day = _local_day(ts, loc_tz.get(oloc, "UTC"))
                if not day:
                    continue
                for li in order.get("line_items") or []:
                    stats["lineItems"] += 1
                    if (li.get("item_type") or "ITEM") in _SKIP_LINE_ITEM_TYPES:
                        continue
                    cid = li.get("catalog_object_id")
                    if not cid:
                        # An amount rung up by hand at the register. Real revenue, but not a
                        # product anything can be forecast for.
                        stats["adHocLineItems"] += 1
                        continue
                    meta = catalog.get(cid)
                    if meta is None:
                        # Sold from a catalog object that no longer exists (deleted item).
                        # Keep the demand — it happened — under its stable Square id.
                        stats["unmappedCatalogObjects"] += 1
                        meta = {"sku": cid, "has_sku": False,
                                "name": li.get("name") or cid, "category": None}
                    if not meta.get("has_sku", True):
                        stats["skusWithoutSkuField"].add(meta["sku"])

                    qty = _num(li.get("quantity"), 0.0)
                    if qty <= 0:
                        continue
                    # gross_sales_money is pre-discount and pre-tax; total_discount_money is
                    # what came off this line. Net them to the price actually PAID per unit
                    # and flag the day as on-promotion, so the model sees a genuine price dip
                    # rather than a mystery demand spike. Same rule as the Shopify source.
                    gross = _money(li, "gross_sales_money", "amount")
                    if gross is None:
                        base = _money(li, "base_price_money", "amount")
                        gross = base * qty if base is not None else None
                    disc = _money(li, "total_discount_money", "amount") or 0.0
                    paid = ((gross - disc) / qty) if (gross is not None and qty) else None

                    name = li.get("name") or meta.get("name") or meta["sku"]
                    if li.get("variation_name") and li["variation_name"] not in name:
                        name = f"{name} ({li['variation_name']})"
                    records.append({
                        "date": day, "sku": meta["sku"], "sku_name": meta.get("name") or name,
                        "units_sold": qty,
                        "price": round(paid, 2) if paid is not None else None,
                        "on_promotion": 1 if disc > 0 else 0,
                        "location_id": oloc,
                        "category": meta.get("category"),
                    })
            cursor = body.get("cursor")
            if not cursor:
                break

    stats["skusWithoutSkuField"] = len(stats["skusWithoutSkuField"])
    if not records:
        raise SquareError(
            "No completed orders came back from Square for the requested window. "
            "Check the date range, that the token can see these locations, and that the "
            "environment (production vs sandbox) is right.")

    df = pd.DataFrame(records)
    # Pool locations into one series per SKU. This is the single-location model the catalog
    # currently expects; see SQUARE_AND_LOCATIONS.md Part 2 for why a two-store account
    # eventually wants one series per (SKU, location) instead. location_id is preserved on
    # the pre-aggregation rows above so that becomes a grouping change, not a re-fetch.
    # One row per product per day. The day's price is the units-weighted average of what
    # was paid, and a day that sold at genuinely different prices is FLAGGED rather than
    # silently assigned whichever price rang up last. See forecast_engine.daily_price_rollup.
    df = daily_price_rollup(df)

    # THE WINDOW ENDS AT THE LAST COMPLETE DAY — NOT TODAY.
    #
    # Filling through today asserts a whole day of no sales for a day that is still
    # happening. The sync runs at 00:15, so "today" is fifteen minutes old at fetch time,
    # and every single day the current day was entering the training series as a
    # confident zero. On a product selling four a day that drags the rate down and
    # manufactures intermittency out of nothing.
    #
    # A day inside the window with no order is an observed zero. A day still in progress
    # is not observed at all, so it is excluded entirely — both from the fill and from
    # the fetched rows, which would otherwise contribute a partial count. Today's sales
    # are tracked separately (see today_sales in main.py) for display and for decrementing
    # a manual stock count; they never reach the model.
    if include_today:
        # The ONE caller that wants the day in progress: the hourly today-so-far tick.
        # Returned raw — no completeness filter and no zero-fill, because neither makes
        # sense for a partial day. This frame must never reach _ingest; see today_sales.py.
        df.attrs["square"] = {**stats, "partialDay": True,
                              "localDay": str(_local_now_day(loc_tz))}
        return df
    _last_complete = _last_complete_day(loc_tz)
    df = df[pd.to_datetime(df["date"]).dt.date <= _last_complete]
    if df.empty:
        raise SquareError(
            "Square returned orders, but none from a completed day yet. If this store only "
            "started selling today, there is nothing to forecast from until tomorrow.")
    df = _fill_zero_days(df, window_end=_last_complete)
    stats["rows"] = int(len(df))
    stats["skus"] = int(df["sku"].nunique())
    df.attrs["square"] = stats
    return df


def _local_now_day(loc_tz=None):
    """Today, at the store. The EARLIEST local day across locations, matching
    _last_complete_day, so the two can never name overlapping days."""
    import zoneinfo
    now = datetime.datetime.now(datetime.timezone.utc)
    names = [t for t in (loc_tz or {}).values() if t] or ["UTC"]
    days = []
    for name in names:
        try:
            days.append(now.astimezone(zoneinfo.ZoneInfo(name)).date())
        except Exception:                                   # noqa: BLE001
            days.append(now.date())
    return min(days)


def _last_complete_day(loc_tz=None):
    """The most recent day that has finished AT THE STORE.

    In the store's own timezone, for the same reason _local_day exists: a shop in
    America/New_York is still on the 24th when UTC has moved to the 25th, so a UTC-based
    answer would discard a completed day of that store's trade — or keep a day that has
    not finished. With several locations the EARLIEST local day wins, because a day is
    only complete once it is complete everywhere the sales come from.
    """
    import zoneinfo
    now = datetime.datetime.now(datetime.timezone.utc)
    names = [t for t in (loc_tz or {}).values() if t] or ["UTC"]
    days = []
    for name in names:
        try:
            days.append(now.astimezone(zoneinfo.ZoneInfo(name)).date())
        except Exception:                                   # noqa: BLE001 — unknown tz
            days.append(now.date())
    return min(days) - datetime.timedelta(days=1)


def _local_day(ts, tz_name):
    """RFC3339 UTC timestamp → the calendar day it was AT THE STORE.

    This is not cosmetic. A 9pm Saturday sale in America/New_York is Sunday in UTC, and
    everything downstream aggregates to a calendar day and reads weekly seasonality off
    it. For a shop with evening trade, skipping this moves a meaningful share of sales by
    a day and smears the weekly signal the Prophet route depends on.
    """
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        local = dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:                                   # noqa: BLE001 — unknown tz string
        local = dt.astimezone(datetime.timezone.utc)
    return local.date().isoformat()


def _fill_zero_days(df: pd.DataFrame, window_end=None) -> pd.DataFrame:
    """Fill in the days that had no sale.

    Square, like Shopify, only returns line items for days that HAD an order, so every
    zero-sale day is simply ABSENT. Handing that to the engine makes it average demand
    over sale-days only — it never sees the zeros — so it badly OVER-forecasts, and the
    demand classifier can't detect intermittency because ADI comes out ~1 for everything.

    This matters MORE for Square than it did for Shopify: a physical shop has slow movers
    that sell a handful of times a month, and those are exactly the SKUs the router should
    be sending down the Croston/TSB path.

    EVERY SERIES ENDS AT `window_end`, NOT AT ITS OWN LAST SALE.

    This used to reindex each SKU from its first sale to ITS OWN last sale, which meant a
    product that stopped selling had a series ending on the day it stopped. The silence
    since was not zero-filled; it did not exist. Consequences, all from that one line:

      · `trailing_zero_run` was structurally ALWAYS 0 on live data, because a series could
        not end in a zero. So `is_dormant` could never fire, and a discontinued product
        went on being forecast at its old rate forever — the exact failure the comment in
        forecast_engine.sparse_subtype warns about.
      · The demand rate was averaged over sale-days-plus-internal-gaps only, roughly
        doubling it for anything that had gone quiet.
      · The forecast was generated from the day after the last training row, so a product
        silent for eight weeks produced eight weeks of forecast dated in the PAST.

    A day inside the fetched window with no order is a real, observed zero. Only days
    AFTER the window are unknown, and those are still left out.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    end = pd.to_datetime(window_end).normalize() if window_end is not None else None
    filled = []
    for (sku, sku_name), g in df.groupby(["sku", "sku_name"], sort=False):
        g = g.set_index("date").sort_index()
        g = g[~g.index.duplicated(keep="last")]
        stop = g.index.max() if end is None else max(g.index.max(), end)
        full = pd.date_range(g.index.min(), stop, freq="D")
        g = g.reindex(full)
        g["units_sold"] = g["units_sold"].fillna(0)
        g["on_promotion"] = g["on_promotion"].fillna(0)
        # Zero-sale days carry the last SINGLE price, never a mixed day's blend.
        g = carry_listed_price(g)
        g["category"] = g["category"].ffill().bfill()
        g["sku"], g["sku_name"] = sku, sku_name
        g.index.name = "date"
        filled.append(g.reset_index())
    out = pd.concat(filled, ignore_index=True)
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out[["date", "sku", "sku_name", "units_sold", "price", "price_mixed",
                "on_promotion", "category"]]


# ─── Inventory + catalog metadata ────────────────────────────────────────────

def fetch_catalog_meta(access_token=None, environment=None, api_version=None,
                       location_ids=None, session=None, catalog=None) -> dict:
    """Per-SKU category, CURRENT inventory and per-unit cost.

    Returns {sku: {"category": str|None, "stock": int|None, "cost": float|None}} — the
    same shape shopify_source.fetch_catalog_meta returns, so _ingest's stock_override /
    cost_override path works unchanged.

    Inventory is summed across the requested locations, which is correct for the
    single-location model and is the first thing that has to change for multi-location:
    Square tracks counts per location natively, so the split costs nothing extra to fetch.

    `cost` is usually None — see _vendor_cost.
    """
    token, env, ver, _, wanted = _cfg(access_token, environment, api_version, None, location_ids)
    locations = fetch_locations(token, env, ver, session=session)
    loc_ids = _active_location_ids(locations, wanted)
    pricing = {}
    if catalog is None:
        catalog = fetch_catalog(token, env, ver, session=session, pricing_out=pricing)

    counts = _inventory_counts(token, env, ver, list(catalog.keys()), loc_ids, session=session)
    loc_tz = {l["id"]: l.get("timezone") for l in locations if l["id"] in loc_ids}
    local_day = _local_now_day(loc_tz).isoformat()

    meta = {}
    for cid, info in catalog.items():
        sku = info["sku"]
        entry = meta.setdefault(sku, {"category": info.get("category"),
                                      "stock": None, "cost": info.get("cost")})
        # What the shelf says today: the listed price, and the price after any automatic
        # discount in force. First variation carrying a price wins for a shared SKU.
        if entry.get("listPrice") is None:
            lp = _variation_price(info, loc_ids)
            if lp is not None and lp > 0:
                entry["listPrice"] = round(lp, 2)
                entry["currentPrice"] = round(lp, 2)
                disc = active_discount({**info, "id": cid}, pricing, local_day, lp)
                if disc:
                    entry.update({"currentPrice": disc["price"], "discountName": disc["name"],
                                  "discountFrom": disc["from"], "discountUntil": disc["until"]})
        if cid in counts:
            entry["stock"] = (entry["stock"] or 0) + counts[cid]
        if entry.get("category") is None:
            entry["category"] = info.get("category")
        if entry.get("cost") is None:
            entry["cost"] = info.get("cost")
    return meta


def _inventory_counts(token, env, ver, catalog_object_ids, loc_ids, session=None) -> dict:
    """{catalog_object_id: units IN_STOCK across loc_ids}. Missing ids simply aren't keyed."""
    totals = {}
    for chunk in _chunks(catalog_object_ids, MAX_CATALOG_IDS_PER_QUERY):
        cursor = None
        while True:
            payload = {"catalog_object_ids": chunk, "location_ids": loc_ids,
                       "states": ["IN_STOCK"]}
            if cursor:
                payload["cursor"] = cursor
            body = _request("POST", "/v2/inventory/counts/batch-retrieve", token, ver, env,
                            json_body=payload, session=session)
            for c in body.get("counts") or []:
                if (c.get("state") or "IN_STOCK") != "IN_STOCK":
                    continue
                cid = c.get("catalog_object_id")
                if not cid:
                    continue
                totals[cid] = totals.get(cid, 0) + _num(c.get("quantity"), 0.0)
            cursor = body.get("cursor")
            if not cursor:
                break
    return {k: int(round(v)) for k, v in totals.items()}


def sample_stock(access_token=None, environment=None, api_version=None,
                 location_ids=None, session=None) -> dict:
    """{sku: on_hand} for StockLog. Thin wrapper so the source registry has one name for it."""
    meta = fetch_catalog_meta(access_token, environment, api_version, location_ids, session=session)
    return {sku: m["stock"] for sku, m in meta.items() if m.get("stock") is not None}


if __name__ == "__main__":
    out = fetch_sales()
    print(out.head(20).to_string(index=False))
    info = out.attrs.get("square", {})
    print(f"\n{len(out):,} daily rows across {out['sku'].nunique()} SKUs "
          f"from {len(info.get('locations', []))} location(s).")
    if info.get("adHocLineItems"):
        print(f"{info['adHocLineItems']:,} ad-hoc line items skipped (no catalog object).")
    if info.get("skusWithoutSkuField"):
        print(f"{info['skusWithoutSkuField']:,} products have no SKU set in Square "
              f"— using their Square id instead.")
