"""
test_attributes_source.py — the Grouping tab's AI tags never replace the file's own.

Backlog batch 12: the classifier saved AI categories exactly like a person's edit, so
opening the Grouping tab replaced the Category from the file, and every later import
kept the AI's guess over the file.

Run:  python test_attributes_source.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("LOGITRACK_PERSIST", "0")
_tmp = tempfile.mkdtemp()
for _k, _f in (("LOGITRACK_CONNECTIONS", "connections.json"), ("LOGITRACK_WORKSPACES", "ws"),
               ("LOGITRACK_LIVE_PRICES_PATH", "live_prices.json")):
    os.environ.setdefault(_k, os.path.join(_tmp, _f))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd   # noqa: E402
import main as M      # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILURES.append(name)


M._rebuild = lambda *a, **k: None          # grouping only; no model fits
M._compute_groups = lambda: {"groupColumns": [], "skus": []}
df = pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=10), "y": 1.0})
with M._state_lock:
    M._catalog.clear()
    M._catalog["MUG"] = {"df": df, "attrs": {"category": "Drinkware"}, "sku_name": "Mug"}
    M._catalog["NEW"] = {"df": df, "attrs": {}, "sku_name": "New thing"}

print("the AI fills in, it doesn't replace")
M.set_attributes({"skus": {"MUG": {"category": "Kitchen", "color": "blue"},
                           "NEW": {"category": "Gadgets"}}, "source": "ai"})
c = M._catalog
check("the file's Category stays", c["MUG"]["attrs"].get("category") == "Drinkware", c["MUG"]["attrs"])
check("an empty attribute is filled", c["MUG"]["attrs"].get("color") == "blue", c["MUG"]["attrs"])
check("a product with no category gets the AI's", c["NEW"]["attrs"].get("category") == "Gadgets")
check("nothing the AI set counts as set by hand",
      not c["MUG"].get("attrs_set") and not c["NEW"].get("attrs_set"), (c["MUG"].get("attrs_set"), c["NEW"].get("attrs_set")))
M.set_attributes({"skus": {"NEW": {"category": "Tools"}}, "source": "ai"})
check("a later AI pass can revise its own guess", c["NEW"]["attrs"].get("category") == "Tools")

print("\na person's edit wins")
M.set_attributes({"skus": {"MUG": {"category": "Kitchen"}}})
check("a hand edit replaces the file's value", c["MUG"]["attrs"].get("category") == "Kitchen")
check("and is recorded as set by hand", "category" in (c["MUG"].get("attrs_set") or []))
M.set_attributes({"skus": {"MUG": {"category": "Barware"}}, "source": "ai"})
check("the AI can't undo it", c["MUG"]["attrs"].get("category") == "Kitchen", c["MUG"]["attrs"])

print(f"\n{'All attribute source tests passed.' if not FAILURES else f'{len(FAILURES)} FAILED: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
