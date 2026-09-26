"""
test_square_source.py — offline verification of the Square connector.

Same discipline as test_shopify_source.py: every API response is a fixture, nothing
touches the network, and the tests pin the four behaviours that are genuinely easy to get
wrong and silently wrong — cents, string quantities, per-location timezone day
assignment, and zero-sale-day filling.

Run:  python test_square_source.py
"""
from __future__ import annotations

import json
import sys

import square_source as SQ
import pandas as _pd
import datetime as _dt


# ─── A fake Square ───────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSquare:
    """Routes by (method, path). `queue` lets a path return a sequence of responses so
    pagination and rate-limit retries can be exercised."""

    def __init__(self, routes):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        path = url.split("squareup.com")[-1].split("squareupsandbox.com")[-1]
        key = f"{method} {path}"
        self.calls.append({"key": key, "body": json, "params": params, "headers": headers})
        seq = self.routes.get(key)
        if not seq:
            raise AssertionError(f"Unexpected call {key}; known: {sorted(self.routes)}")
        payload = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(payload, tuple):
            return FakeResponse(payload[0], payload[1])
        return FakeResponse(payload)


LOCATIONS = {"locations": [
    {"id": "LOC_ATL", "name": "Alpharetta", "timezone": "America/New_York",
     "currency": "USD", "status": "ACTIVE"},
    {"id": "LOC_LA", "name": "Westside", "timezone": "America/Los_Angeles",
     "currency": "USD", "status": "ACTIVE"},
    {"id": "LOC_OLD", "name": "Closed store", "timezone": "America/New_York",
     "currency": "USD", "status": "INACTIVE"},
]}

CATALOG = {"objects": [
    {"type": "CATEGORY", "id": "CAT_MUG", "category_data": {"name": "Drinkware"}},
    {"type": "ITEM", "id": "ITEM_1", "item_data": {
        "name": "Travel Mug", "category_id": "CAT_MUG",
        "variations": [
            {"id": "VAR_1", "item_variation_data": {
                "item_id": "ITEM_1", "name": "12oz", "sku": "MUG-12",
                "price_money": {"amount": 1899, "currency": "USD"}}},
            # No SKU set — must fall back to the catalog object id.
            {"id": "VAR_2", "item_variation_data": {
                "item_id": "ITEM_1", "name": "16oz", "sku": "",
                "price_money": {"amount": 2199, "currency": "USD"}}},
        ]}},
    {"type": "ITEM", "id": "ITEM_2", "item_data": {
        "name": "Tote Bag",
        "reporting_category": {"id": "CAT_MUG"},
        "variations": [
            {"id": "VAR_3", "item_variation_data": {
                "item_id": "ITEM_2", "name": "Regular", "sku": "TOTE-1",
                "price_money": {"amount": 2500, "currency": "USD"}}},
        ]}},
]}


def _order(oid, loc, closed_at, line_items):
    return {"id": oid, "location_id": loc, "state": "COMPLETED",
            "closed_at": closed_at, "created_at": closed_at, "line_items": line_items}


def _li(cid, name, qty, gross_cents, discount_cents=0, item_type="ITEM"):
    li = {"uid": f"u{cid}{qty}", "name": name, "quantity": str(qty), "item_type": item_type,
          "gross_sales_money": {"amount": gross_cents, "currency": "USD"},
          "total_discount_money": {"amount": discount_cents, "currency": "USD"}}
    if cid:
        li["catalog_object_id"] = cid
    return li


ORDERS = {"orders": [
    # 2026-03-02T01:30:00Z is 8:30pm on MARCH 1 in America/New_York. If the day is taken
    # from the UTC string this lands on the 2nd and the weekly seasonal signal smears.
    _order("O1", "LOC_ATL", "2026-03-02T01:30:00Z", [
        _li("VAR_1", "Travel Mug", 2, 3798),
        _li(None, "Misc", 1, 800),                       # ad-hoc: dropped, counted
    ]),
    _order("O2", "LOC_ATL", "2026-03-03T15:00:00Z", [
        _li("VAR_1", "Travel Mug", 1, 1899, 300),        # discounted → on_promotion
        _li("VAR_2", "Travel Mug 16oz", 3, 6597),
    ]),
    # Five days later, so the zero-fill has a gap to close.
    _order("O3", "LOC_LA", "2026-03-08T20:00:00Z", [
        _li("VAR_3", "Tote Bag", 4, 10000),
        _li("VAR_GC", "Gift card", 1, 5000, item_type="GIFT_CARD"),
    ]),
]}

INVENTORY = {"counts": [
    {"catalog_object_id": "VAR_1", "location_id": "LOC_ATL", "state": "IN_STOCK", "quantity": "12"},
    {"catalog_object_id": "VAR_1", "location_id": "LOC_LA", "state": "IN_STOCK", "quantity": "5"},
    {"catalog_object_id": "VAR_3", "location_id": "LOC_ATL", "state": "IN_STOCK", "quantity": "40"},
    {"catalog_object_id": "VAR_3", "location_id": "LOC_ATL", "state": "SOLD", "quantity": "99"},
]}


def _fake(orders=None, inventory=None, catalog=None, locations=None):
    return FakeSquare({
        "GET /v2/locations": locations or LOCATIONS,
        "GET /v2/catalog/list": catalog or CATALOG,
        "POST /v2/orders/search": orders or ORDERS,
        "POST /v2/inventory/counts/batch-retrieve": inventory or INVENTORY,
    })


# ─── Tests ───────────────────────────────────────────────────────────────────

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_local_day():
    print("_local_day — per-location timezone")
    check("evening sale stays on the local day",
          SQ._local_day("2026-03-02T01:30:00Z", "America/New_York") == "2026-03-01")
    check("west coast evening likewise",
          SQ._local_day("2026-03-09T04:00:00Z", "America/Los_Angeles") == "2026-03-08")
    check("midday is unaffected",
          SQ._local_day("2026-03-03T15:00:00Z", "America/New_York") == "2026-03-03")
    check("unknown timezone degrades to UTC rather than raising",
          SQ._local_day("2026-03-03T15:00:00Z", "Not/AZone") == "2026-03-03")
    check("missing timestamp returns None", SQ._local_day(None, "UTC") is None)


def test_money_and_numbers():
    print("money + quantity parsing")
    check("cents become dollars",
          SQ._money({"m": {"amount": 1899}}, "m", "amount") == 18.99)
    check("absent money is None", SQ._money({}, "m", "amount") is None)
    check("string quantity parses", SQ._num("3") == 3.0)
    check("junk quantity falls back", SQ._num(None, 0.0) == 0.0)


def test_catalog():
    print("fetch_catalog")
    cat = SQ.fetch_catalog("tok", "production", session=_fake())
    check("maps variation id to sku", cat["VAR_1"]["sku"] == "MUG-12")
    check("blank sku falls back to the catalog object id", cat["VAR_2"]["sku"] == "VAR_2")
    check("records that the sku was missing", cat["VAR_2"]["has_sku"] is False)
    check("resolves legacy category_id", cat["VAR_1"]["category"] == "Drinkware")
    check("resolves reporting_category", cat["VAR_3"]["category"] == "Drinkware")
    check("builds a readable name", cat["VAR_1"]["name"] == "Travel Mug (12oz)")
    check("price comes through in dollars", cat["VAR_3"]["price"] == 25.0)
    check("cost is None without Retail Premium", cat["VAR_1"]["cost"] is None)


def test_fetch_sales():
    print("fetch_sales")
    df = SQ.fetch_sales("tok", "production", session=_fake())
    stats = df.attrs["square"]

    check("columns match the shared contract",
          list(df.columns) == ["date", "sku", "sku_name", "units_sold", "price",
                               "price_mixed", "on_promotion", "category"],
          list(df.columns))

    mug = df[(df.sku == "MUG-12")].set_index("date")
    check("evening order lands on the local day", "2026-03-01" in mug.index)
    check("units are summed per day", mug.loc["2026-03-01", "units_sold"] == 2)
    check("undiscounted price is dollars", mug.loc["2026-03-01", "price"] == 18.99)
    check("discounted unit price nets the discount out",
          round(float(mug.loc["2026-03-03", "price"]), 2) == 15.99)
    check("discount flags the day as on-promotion",
          mug.loc["2026-03-03", "on_promotion"] == 1)
    check("undiscounted day is not flagged", mug.loc["2026-03-01", "on_promotion"] == 0)

    check("zero-sale days are filled in", mug.loc["2026-03-02", "units_sold"] == 0)
    check("filled day carries the price forward", mug.loc["2026-03-02", "price"] == 18.99)
    check("filled day is not on promotion", mug.loc["2026-03-02", "on_promotion"] == 0)

    # The series must not stop at the last sale. It used to, and that one fact meant a
    # series could never END in a zero — so trailing_zero_run was structurally 0, dormancy
    # could never be detected, and a discontinued product was forecast at its old rate
    # forever while its chart showed weeks of "forecast" dated in the past.
    _end = _pd.to_datetime(df["date"]).max().date()
    _today = _dt.datetime.now(_dt.timezone.utc).date()
    check("the series runs past the last sale, to the window end",
          _end > _dt.date(2026, 3, 3), str(_end))
    # And NOT to today. Filling through a day still in progress asserts a whole day of
    # no sales fifteen minutes after midnight, which put a fake zero on the end of every
    # product's series once a day and quietly dragged its demand rate down.
    check("today is NOT in the series — it has not finished yet",
          _end < _today, f"series ends {_end}, today is {_today}")
    _md = _pd.to_datetime(mug.index)
    check("and everything after the last sale is a real zero",
          float(mug.loc[_md > "2026-03-03", "units_sold"].sum()) == 0.0)

    check("sku-less variation uses its square id", "VAR_2" in set(df.sku))
    check("gift cards are skipped", not any(df.sku_name.str.contains("Gift card", na=False)))

    tote = df[df.sku == "TOTE-1"]
    check("west-coast order uses its own timezone",
          tote.iloc[0]["date"] == "2026-03-08", tote.iloc[0]["date"])

    check("ad-hoc line items are counted", stats["adHocLineItems"] == 1)
    check("sku-less products are counted", stats["skusWithoutSkuField"] == 1)
    check("locations are reported", set(stats["locations"]) == {"Alpharetta", "Westside"})
    check("inactive locations are not queried", "Closed store" not in stats["locations"])


def test_search_request_shape():
    print("SearchOrders request shape")
    fake = _fake()
    SQ.fetch_sales("tok", "production", days=90, session=fake)
    call = next(c for c in fake.calls if c["key"] == "POST /v2/orders/search")
    q = call["body"]["query"]
    check("filters to completed orders only",
          q["filter"]["state_filter"]["states"] == ["COMPLETED"])
    check("sorts by CLOSED_AT to match the date filter",
          q["sort"]["sort_field"] == "CLOSED_AT")
    check("date filter is on closed_at", "closed_at" in q["filter"]["date_time_filter"])
    check("only active locations are requested",
          set(call["body"]["location_ids"]) == {"LOC_ATL", "LOC_LA"})
    check("omits Square-Version unless one was set",
          "Square-Version" not in (call["headers"] or {}))

    fake2 = _fake()
    SQ.fetch_sales("tok", "production", api_version="2026-01-21", session=fake2)
    call2 = next(c for c in fake2.calls if c["key"] == "POST /v2/orders/search")
    check("sends Square-Version when one is set",
          call2["headers"].get("Square-Version") == "2026-01-21")


def test_no_date_filter_when_all_history():
    print("full-history pull")
    fake = _fake()
    SQ.fetch_sales("tok", "production", days=None, session=fake)
    call = next(c for c in fake.calls if c["key"] == "POST /v2/orders/search")
    check("omits the date filter entirely",
          "date_time_filter" not in call["body"]["query"]["filter"])


def test_fill_zero_days_window_end():
    """The fill contract, against a FIXED end so the assertion cannot drift with time."""
    print("zero-fill runs to the window end")
    fx = _pd.DataFrame([{"date": "2026-03-01", "sku": "Q", "sku_name": "Q", "units_sold": 2,
                         "price": 10.0, "on_promotion": 0, "category": "C"}])
    out = SQ._fill_zero_days(fx, window_end="2026-03-10")
    check("fills through to the window end", len(out) == 10, str(len(out)))
    check("the tail is zeros, not repeats", float(out["units_sold"].sum()) == 2.0,
          str(out["units_sold"].tolist()))
    check("price carries across the silent tail", float(out["price"].iloc[-1]) == 10.0)
    check("category carries too", out["category"].iloc[-1] == "C")

    # A product still selling at the window end is unaffected.
    fx2 = _pd.DataFrame([{"date": d, "sku": "R", "sku_name": "R", "units_sold": 1,
                          "price": 5.0, "on_promotion": 0, "category": "C"}
                         for d in ("2026-03-08", "2026-03-10")])
    check("a still-selling product gains no tail",
          len(SQ._fill_zero_days(fx2, window_end="2026-03-10")) == 3)
    check("no window end -> ends at the last sale (old behaviour, still reachable)",
          len(SQ._fill_zero_days(fx)) == 1)


def test_mixed_price_days():
    """A day counts at the price most of its units sold at; it is mixed only when no
    price covers 80% of units. One coupon must not throw a busy day away."""
    print("a day counts at its shelf price; mixed only without an 80% majority")
    import forecast_engine as FE
    def L(date, q, p, promo=0):
        return {"date": date, "sku": "M", "sku_name": "M", "units_sold": q, "price": p,
                "on_promotion": promo, "category": "C"}
    day = FE.daily_price_rollup(_pd.DataFrame([
        L("2026-09-01", 29, 50.0), L("2026-09-01", 1, 40.0, 1),      # one coupon in 30
        L("2026-09-02", 20, 50.0), L("2026-09-02", 10, 40.0, 1),     # a third discounted
        L("2026-09-03", 2, 49.99), L("2026-09-03", 1, 50.00),        # cent rounding
        L("2026-09-04", 4, 40.0), L("2026-09-04", 6, 20.0, 1),       # buy-one-get-one day
    ]))
    r = {row.date: row for row in day.itertuples()}
    check("one coupon in 30 units: the day counts at the $50 shelf price",
          r["2026-09-01"].price == 50.0 and r["2026-09-01"].price_mixed == 0, r["2026-09-01"])
    check("and it isn't marked as a promotion day", r["2026-09-01"].on_promotion == 0)
    check("a third discounted: mixed, at the average paid", r["2026-09-02"].price_mixed == 1
          and abs(r["2026-09-02"].price - 46.67) < 0.01, r["2026-09-02"])
    check("cent-rounding noise is one price", r["2026-09-03"].price_mixed == 0)
    check("a buy-one-get-one day with no majority is mixed", r["2026-09-04"].price_mixed == 1)
    check("units still add up", int(r["2026-09-01"].units_sold) == 30)

    # A mixed day, then silence: the silent days carry the last SHELF price, not the blend.
    seq = FE.daily_price_rollup(_pd.DataFrame([
        L("2026-09-01", 2, 50.0), L("2026-09-03", 3, 50.0), L("2026-09-03", 3, 40.0, 1)]))
    out = SQ._fill_zero_days(seq, window_end="2026-09-06")
    tail = out[out["date"] > "2026-09-03"]
    check("price_mixed survives the fill", "price_mixed" in out.columns)
    check("zero-sale days after a mixed day carry the last shelf price ($50), not $45",
          set(tail["price"].round(2)) == {50.0}, tail["price"].tolist())
    check("zero-sale days are never mixed", int(tail["price_mixed"].sum()) == 0)


def test_inventory():
    print("fetch_catalog_meta")
    meta = SQ.fetch_catalog_meta("tok", "production", session=_fake())
    check("stock sums across locations", meta["MUG-12"]["stock"] == 17)
    check("non-IN_STOCK states are ignored", meta["TOTE-1"]["stock"] == 40)
    check("category rides along", meta["MUG-12"]["category"] == "Drinkware")
    check("cost is None when unavailable", meta["MUG-12"]["cost"] is None)
    check("a product with no count has stock None", meta["VAR_2"]["stock"] is None)

    levels = SQ.sample_stock("tok", "production", session=_fake())
    check("sample_stock drops the unknowns", "VAR_2" not in levels)
    check("sample_stock keeps the knowns", levels["MUG-12"] == 17)


def test_shelf_price_and_automatic_discounts():
    """The price on the shelf today, including discounts Square applies by itself."""
    print("listed price, location overrides and automatic discounts")
    import copy
    cat = copy.deepcopy(CATALOG)
    # Tote Bag is $25 everywhere but $22 at the Alpharetta store.
    tote = cat["objects"][2]["item_data"]["variations"][0]["item_variation_data"]
    tote["location_overrides"] = [{"location_id": "LOC_ATL", "price_money": {"amount": 2200}}]
    cat["objects"] += [
        {"type": "DISCOUNT", "id": "D_MUG", "discount_data": {
            "name": "Mug month", "discount_type": "FIXED_PERCENTAGE", "percentage": "25"}},
        {"type": "PRODUCT_SET", "id": "PS_MUG", "product_set_data": {"product_ids_any": ["CAT_MUG"]}},
        {"type": "PRICING_RULE", "id": "R_MUG", "pricing_rule_data": {
            "name": "Mug month", "discount_id": "D_MUG", "match_products_id": "PS_MUG",
            "valid_until_date": "2099-12-31"}},
        # Needs a PIN at the register — a price some checkouts get, not the shelf price.
        {"type": "DISCOUNT", "id": "D_PIN", "discount_data": {
            "name": "Staff", "discount_type": "FIXED_PERCENTAGE", "percentage": "50",
            "pin_required": True}},
        {"type": "PRICING_RULE", "id": "R_PIN", "pricing_rule_data": {
            "discount_id": "D_PIN", "match_products_id": "PS_MUG"}},
        # Expired: must not count.
        {"type": "DISCOUNT", "id": "D_OLD", "discount_data": {
            "name": "Old sale", "discount_type": "FIXED_AMOUNT", "amount_money": {"amount": 1000}}},
        {"type": "PRICING_RULE", "id": "R_OLD", "pricing_rule_data": {
            "discount_id": "D_OLD", "match_products_id": "PS_MUG", "valid_until_date": "2020-01-01"}},
    ]
    meta = SQ.fetch_catalog_meta("tok", "production", session=_fake(catalog=cat))
    mug = meta["MUG-12"]
    check("listed price read from the variation", mug["listPrice"] == 18.99, mug)
    check("an automatic discount on the category lowers today's price",
          mug["currentPrice"] == round(18.99 * 0.75, 2), mug)
    check("and names it, with its end date", mug.get("discountName") == "Mug month"
          and mug.get("discountUntil") == "2099-12-31", mug)
    check("PIN-only and expired discounts are ignored", mug["currentPrice"] > 18.99 * 0.5)
    tote_m = meta["TOTE-1"]
    check("a location's own price overrides the base price", tote_m["listPrice"] == 22.0, tote_m)


def test_pagination():
    print("pagination")
    page1 = {"orders": ORDERS["orders"][:1], "cursor": "CUR1"}
    page2 = {"orders": ORDERS["orders"][1:]}
    fake = _fake(orders=[page1, page2])
    df = SQ.fetch_sales("tok", "production", session=fake)
    searches = [c for c in fake.calls if c["key"] == "POST /v2/orders/search"]
    check("follows the cursor", len(searches) == 2, f"{len(searches)} calls")
    check("second page sends the cursor", searches[1]["body"].get("cursor") == "CUR1")
    check("both pages land in the frame", "TOTE-1" in set(df.sku))


def test_rate_limit_retry():
    print("rate limiting")
    throttled = ({"errors": [{"category": "RATE_LIMIT_ERROR", "code": "RATE_LIMITED"}]}, 200)
    fake = _fake(orders=[throttled, ORDERS])
    SQ.time.sleep = lambda *_: None                      # no real waiting in a test
    df = SQ.fetch_sales("tok", "production", session=fake)
    check("retries a throttled body and succeeds", len(df) > 0)

    fake429 = _fake(orders=[({}, 429), ORDERS])
    df2 = SQ.fetch_sales("tok", "production", session=fake429)
    check("retries an HTTP 429 and succeeds", len(df2) > 0)


def test_errors():
    print("error handling")
    for status, needle in ((401, "rejected the token"), (403, "rejected the token"),
                           (404, "environment")):
        fake = _fake(orders=({}, status))
        try:
            SQ.fetch_sales("tok", "production", session=fake)
            check(f"HTTP {status} raises", False)
        except SQ.SquareError as e:
            check(f"HTTP {status} explains itself", needle in str(e), str(e))

    fake = _fake(orders={"orders": []})
    try:
        SQ.fetch_sales("tok", "production", session=fake)
        check("empty result raises", False)
    except SQ.SquareError as e:
        check("empty result explains itself", "No completed orders" in str(e))

    fake = _fake(locations={"locations": []})
    try:
        SQ.fetch_locations("tok", "production", session=fake)
        check("no locations raises", False)
    except SQ.SquareError as e:
        check("no locations explains itself", "no locations" in str(e))

    try:
        SQ.fetch_sales("tok", "production", location_ids=["NOPE"], session=_fake())
        check("unknown location raises", False)
    except SQ.SquareError as e:
        check("unknown location lists what's available",
              "NOPE" in str(e) and "Alpharetta" in str(e))

    try:
        SQ.fetch_sales(None, "production", session=_fake())
        check("missing token raises", False)
    except SQ.SquareError as e:
        check("missing token explains itself", "access token is required" in str(e))


def test_explicit_locations():
    print("explicit location selection")
    fake = _fake()
    SQ.fetch_sales("tok", "production", location_ids=["LOC_ATL"], session=fake)
    call = next(c for c in fake.calls if c["key"] == "POST /v2/orders/search")
    check("only the chosen location is queried", call["body"]["location_ids"] == ["LOC_ATL"])


def test_wrong_credential_is_named():
    print("pasting the wrong credential says which one")
    # The exact mix-up Square's own Credentials page invites: two values, one of them useless.
    for app_id in ("sq0idp-LJ1Sr4Iim0hGGvsMrx83vF",
                   "sandbox-sq0idb-ioiyW39PwrXoGyLt4",
                   "SQ0IDP-UpperCasePasted"):
        try:
            SQ.fetch_sales(app_id, "production", session=_fake())
            check(f"{app_id[:12]} is rejected", False)
        except SQ.SquareError as e:
            check(f"{app_id[:12]} is named as an Application ID",
                  "Application ID" in str(e) and "Access token" in str(e), str(e))

    try:
        SQ.fetch_sales("shpat_abc123", "production", session=_fake())
        check("a Shopify token is rejected", False)
    except SQ.SquareError as e:
        check("a Shopify token says so", "Shopify" in str(e), str(e))

    # A real token still works, and surrounding whitespace from a paste is forgiven.
    df = SQ.fetch_sales("  EAAAlreal_token_here  ", "production", session=_fake())
    check("a real token is accepted", len(df) > 0)
    fake = _fake()
    SQ.fetch_locations(" EAAAtoken ", "production", session=fake)
    check("whitespace is stripped before the header is built",
          fake.calls[0]["headers"]["Authorization"] == "Bearer EAAAtoken",
          fake.calls[0]["headers"]["Authorization"])


def test_sandbox_base_url():
    print("sandbox routing")
    fake = _fake()
    SQ.fetch_locations("tok", "sandbox", session=fake)
    check("sandbox calls are routed to the sandbox host", len(fake.calls) == 1)
    try:
        SQ._cfg("tok", "staging")
        check("unknown environment raises", False)
    except SQ.SquareError as e:
        check("unknown environment explains itself", "production" in str(e))


if __name__ == "__main__":
    import os
    # Env vars must not leak into the fixtures.
    for k in ("SQUARE_ACCESS_TOKEN", "SQUARE_ENVIRONMENT", "SQUARE_DAYS",
              "SQUARE_LOCATION_IDS", "SQUARE_API_VERSION"):
        os.environ.pop(k, None)

    for fn in (test_local_day, test_money_and_numbers, test_catalog, test_fetch_sales,
               test_search_request_shape, test_no_date_filter_when_all_history,
               test_fill_zero_days_window_end, test_mixed_price_days, test_inventory, test_shelf_price_and_automatic_discounts, test_pagination, test_rate_limit_retry, test_errors,
               test_explicit_locations, test_wrong_credential_is_named,
               test_sandbox_base_url):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All Square connector tests passed.")
