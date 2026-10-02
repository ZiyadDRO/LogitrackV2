"""
test_exports_orders.py — the full report's Open POs sheet and the supplier export.

Backlog batch 8: the Open POs sheet was always empty (it read a field no forecast view
has), and the supplier export listed the "Unassigned orders" holding ledger as a supplier.

Run:  python test_exports_orders.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exports  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


items = [{"view": {"skuId": "A", "skuName": "Alpha"}, "folder": "Mugs"},
         {"view": {"skuId": "B", "skuName": "Beta"}, "folder": None}]
suppliers = [
    {"id": "s1", "name": "Acme", "skuIds": ["A"], "orders": [
        {"id": "o1", "skuId": "A", "qty": 50, "orderedDate": "2026-09-01", "expectedDate": "2026-10-01"},
        {"id": "o2", "skuId": "A", "qty": 30, "orderedDate": "2026-09-12", "expectedDate": "2026-10-12"},
        {"id": "o0", "skuId": "A", "qty": 20, "orderedDate": "2026-08-01", "receivedDate": "2026-08-20"},
    ]},
    {"id": "__unassigned", "name": "Unassigned orders", "skuIds": [], "orders": [
        {"id": "o3", "skuId": "B", "qty": 12, "orderedDate": "2026-09-20", "expectedDate": "2026-10-04"},
    ]},
]
open_pos = {"A": {"orderId": "o1", "qty": 50, "ordered": "2026-09-01", "delivery": "2026-10-01",
                  "supplierId": "s1", "supplier": "Acme"}}

print("Open POs sheet")
rows = exports.open_po_rows(items, open_pos, suppliers)
check("lists every order on the way (PO + two in-transit supplier orders)", len(rows) == 3, rows)
check("the PO and its supplier order are one row", sum(1 for r in rows if r["units"] == 50) == 1, rows)
check("names the supplier", any(r["supplier"] == "Acme" and r["sku"] == "A" for r in rows), rows)
check("an order with no supplier says so", any(r["sku"] == "B" and r["supplier"] == "(no supplier)" for r in rows), rows)
check("received orders are not on it", all(r["units"] != 20 for r in rows), rows)
legacy = {"A": {"qty": 30, "ordered": "2026-09-12", "delivery": "2026-10-12", "supplier": "Acme"}}
rows = exports.open_po_rows(items, legacy, suppliers)
check("a PO saved before the link still isn't listed twice", sum(1 for r in rows if r["units"] == 30) == 1, rows)
wb = exports.build_all_workbook(items, suppliers, [], backtest={}, open_pos=open_pos)
vals = [c.value for row in wb["Open POs"].iter_rows() for c in row]
check("the workbook sheet has them", "Alpha" in vals and 50 in vals, vals[:30])

print("\nsupplier export")
wb = exports.build_suppliers_workbook(suppliers)
names = [r[0].value for r in wb["Suppliers"].iter_rows()]
check("the holding ledger is not a supplier row", "Unassigned orders" not in names, names)
check("Acme is", "Acme" in names, names)
hist = [r[0].value for r in wb["Order History"].iter_rows()]
check("its orders are still in the history, as (no supplier)", "(no supplier)" in hist, hist)

print(f"\n{'All export order tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
