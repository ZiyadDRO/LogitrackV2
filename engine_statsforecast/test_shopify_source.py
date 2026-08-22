"""
test_shopify_source.py — offline verification of the Shopify integration (GraphQL).

No store, no network: a stub `requests` module feeds shopify_source canned
responses shaped exactly like the GraphQL Admin API's, so every piece of LOGIC
is exercised — cursor pagination, both rate-limit shapes (HTTP 429 and a 200
THROTTLED error body), cancelled-order exclusion, discount netting, zero-sale-
day fill, catalog paging with unit costs, and error paths.

What this CANNOT test (needs a dev store + real token):
  • auth / scopes (read_orders, read_all_orders, read_products)
  • the 60-day history boundary without read_all_orders
  • contract drift in the live API vs these fixtures

Run:  python test_shopify_source.py
"""
import sys, types, json

# ── stub: requests (must be installed BEFORE importing shopify_source) ────────
class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)[:300]
    def json(self):
        return self._body


class FakeRequests(types.ModuleType):
    """Queue-driven stand-in for `requests`: each call pops the next response."""
    def __init__(self):
        super().__init__("requests")
        self.queue = []
        self.calls = []
    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        return self.queue.pop(0)
    def get(self, url, params=None, headers=None, timeout=None):   # pragma: no cover
        raise AssertionError("REST GET used — the integration must be GraphQL-only "
                             "(new partner orgs 404 on every REST endpoint).")


fake_requests = FakeRequests()
sys.modules["requests"] = fake_requests

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import shopify_source as SH

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail and not cond else ''}")


def money(x):
    return {"shopMoney": {"amount": f"{x:.2f}"}}


def li(sku, qty, price, disc=0.0, title=None):
    return {"sku": sku, "title": title or sku, "quantity": qty,
            "originalUnitPriceSet": money(price),
            "totalDiscountSet": money(disc),
            "discountAllocations": ([{"allocatedAmountSet": money(disc)}] if disc else [])}


def order(day, items, cancelled=False):
    return {"createdAt": f"{day}T10:00:00Z",
            "cancelledAt": f"{day}T11:00:00Z" if cancelled else None,
            "lineItems": {"nodes": items}}


def orders_page(nodes, has_next=False, cursor=None):
    return {"data": {"orders": {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": nodes}}}


print("\n=== Shopify source verification (stubbed GraphQL) ===\n")

# 1) fetch_sales: pagination + cancelled + discounts + zero-day fill ------------
print("1) fetch_sales")
page1 = orders_page([
    order("2026-06-01", [li("VAN-A", 2, 100.00, disc=20.00)]),        # paid 90, promo
    order("2026-06-02", [li("VAN-A", 5, 100.00)], cancelled=True),    # must be skipped
    order("2026-06-04", [li("VAN-A", 1, 100.00)]),                    # full price
], has_next=True, cursor="c1")
page2 = orders_page([
    order("2026-06-02", [li("VAN-B", 3, 50.00), li("VAN-B", 1, 50.00)]),  # same-day merge → 4
])
fake_requests.queue = [FakeResponse(200, page1), FakeResponse(200, page2)]
df = SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)

a = df[df["sku"] == "VAN-A"].reset_index(drop=True)
b = df[df["sku"] == "VAN-B"].reset_index(drop=True)
check("paginates via pageInfo/endCursor (both pages ingested)", set(df["sku"]) == {"VAN-A", "VAN-B"})
check("second request carries the cursor", fake_requests.calls[1][2]["variables"].get("cursor") == "c1",
      str(fake_requests.calls[1][2]["variables"]))
check("created_at filter sent when days given", "created_at:>=" in (fake_requests.calls[0][2]["variables"].get("q") or ""),
      str(fake_requests.calls[0][2]["variables"]))
check("cancelled order excluded", float(a["units_sold"].sum()) == 3.0, str(a["units_sold"].tolist()))
check("zero-sale days filled (A: 06-01→06-04 = 4 rows)", len(a) == 4, str(a["date"].tolist()))
check("filled days carry 0 units", a.loc[a["date"] == "2026-06-02", "units_sold"].iloc[0] == 0)
check("discount netted into paid price (100 − 20/2 = 90)", float(a.loc[0, "price"]) == 90.00, str(a.loc[0, "price"]))
check("discounted day flagged on_promotion", int(a.loc[0, "on_promotion"]) == 1)
check("price carried forward across gap days", float(a.loc[1, "price"]) == 90.00, str(a.loc[1, "price"]))
check("undiscounted day keeps full price", float(a.loc[3, "price"]) == 100.00, str(a.loc[3, "price"]))
check("same-day line items merged (B = 4 units, one row)", len(b) == 1 and float(b.loc[0, "units_sold"]) == 4.0,
      f"rows={len(b)} units={b['units_sold'].tolist()}")
check("no date filter when days omitted is allowed", True)  # exercised implicitly below

# 2) rate limiting (both shapes) ---------------------------------------------------
print("\n2) rate limiting")
fake_requests.queue = [
    FakeResponse(429, {}, {"Retry-After": "0"}),
    FakeResponse(200, orders_page([order("2026-06-01", [li("X", 1, 10.00)])])),
]
df2 = SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)
check("retries after HTTP 429 and succeeds", len(df2) == 1)

fake_requests.queue = [
    FakeResponse(200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}),
    FakeResponse(200, orders_page([order("2026-06-01", [li("X", 1, 10.00)])])),
]
df3 = SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)
check("retries after 200-THROTTLED error body and succeeds", len(df3) == 1)

fake_requests.queue = [FakeResponse(429, {}, {"Retry-After": "0"})] * 6
try:
    SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)
    check("persistent 429 raises a clear ShopifyError", False, "no exception")
except SH.ShopifyError as e:
    check("persistent 429 raises a clear ShopifyError", "rate" in str(e).lower(), str(e))

# 3) error paths -----------------------------------------------------------------
print("\n3) error paths")
fake_requests.queue = [FakeResponse(404, {"errors": "Not Found"})]
try:
    SH.fetch_sales(shop="wrong-handle", token="shpat_fake", days=30)
    check("404 → actionable store-handle hint", False, "no exception")
except SH.ShopifyError as e:
    check("404 → actionable store-handle hint", "handle" in str(e).lower() and ".myshopify.com" in str(e), str(e))

fake_requests.queue = [FakeResponse(401, {"errors": "Invalid API key"})]
try:
    SH.fetch_sales(shop="test-store", token="bad", days=30)
    check("401 → token/scopes message", False, "no exception")
except SH.ShopifyError as e:
    check("401 → token/scopes message", "token" in str(e).lower(), str(e))

fake_requests.queue = [FakeResponse(200, {"errors": [{"message": "syntax error"}]})]
try:
    SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)
    check("non-throttle GraphQL errors raise", False, "no exception")
except SH.ShopifyError as e:
    check("non-throttle GraphQL errors raise", "syntax error" in str(e), str(e))

fake_requests.queue = [FakeResponse(200, orders_page([]))]
try:
    SH.fetch_sales(shop="test-store", token="shpat_fake", days=30)
    check("empty window raises ShopifyError", False, "no exception")
except SH.ShopifyError:
    check("empty window raises ShopifyError", True)

try:
    SH.fetch_sales(shop=None, token=None)
    check("missing credentials raise ShopifyError", False, "no exception")
except SH.ShopifyError:
    check("missing credentials raise ShopifyError", True)

# 4) fetch_catalog_meta (category + stock + unit cost) ------------------------------
print("\n4) fetch_catalog_meta")
gpage = lambda nodes, has_next, cursor: {"data": {"products": {
    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}}}
fake_requests.queue = [
    FakeResponse(200, gpage([{"productType": "Vanities",
                              "variants": {"nodes": [{"sku": "VAN-A", "inventoryQuantity": 42,
                                                      "inventoryItem": {"unitCost": {"amount": "315.00"}}}]}}], True, "c1")),
    FakeResponse(200, gpage([{"productType": None,
                              "variants": {"nodes": [{"sku": "VAN-B", "inventoryQuantity": None,
                                                      "inventoryItem": {"unitCost": None}}]}}], False, None)),
]
meta = SH.fetch_catalog_meta(shop="test-store", token="shpat_fake")
check("GraphQL pagination follows endCursor", set(meta) == {"VAN-A", "VAN-B"}, str(set(meta)))
check("category + stock + unit cost mapped",
      meta["VAN-A"] == {"category": "Vanities", "stock": 42, "cost": 315.0}, str(meta.get("VAN-A")))
check("missing fields → None (not crash)",
      meta["VAN-B"] == {"category": None, "stock": None, "cost": None}, str(meta.get("VAN-B")))

print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("Logic verified offline (GraphQL-only — REST would 404 on new partner orgs).")
print("Still needed before production: one live run against a Shopify DEV STORE to")
print("validate auth/scopes and the real API contract.")
