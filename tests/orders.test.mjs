// Purchase orders and supplier orders stay linked (backlog batch 8).
// Run:  node tests/orders.test.mjs
import { findPoOrder, poIsOrder, updateOrder, arrivalAddsToStock, todayStr, setStoreZone } from "../src/lib/helpers.js";

let failed = 0;
const check = (name, cond, detail = "") => {
  console.log(`  ${cond ? "ok  " : "FAIL"} ${name}${cond ? "" : " — " + detail}`);
  if (!cond) failed += 1;
};

setStoreZone("America/New_York");
const today = todayStr();
const dayOffset = (n) => { const d = new Date(`${today}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };

const suppliers = {
  s1: { id: "s1", name: "Acme", skuIds: ["A"], orders: [
    { id: "o1", skuId: "A", orderedDate: "2026-09-01", qty: 50, receivedDate: null },
    { id: "o2", skuId: "A", orderedDate: "2026-09-10", qty: 80, receivedDate: null },
    { id: "o0", skuId: "A", orderedDate: "2026-08-01", qty: 20, receivedDate: "2026-08-20" },
  ] },
};

console.log("the open PO finds its own order");
check("by order id", findPoOrder(suppliers, "A", { orderId: "o2", qty: 80 })?.order.id === "o2");
check("an old PO by date and quantity",
  findPoOrder(suppliers, "A", { ordered: "2026-09-10", qty: 80 })?.order.id === "o2");
check("an old PO that matches nothing, with two open orders: no guess",
  findPoOrder(suppliers, "A", { ordered: "2026-09-05", qty: 10 }) === null);
const one = { s1: { ...suppliers.s1, orders: [suppliers.s1.orders[0]] } };
check("an old PO with a single open order: that one",
  findPoOrder(one, "A", { ordered: "2026-09-05", qty: 10 })?.order.id === "o1");

console.log("\nmarking an order received clears only its own PO");
const openPOs = { A: { orderId: "o2", qty: 80 } };
check("o2 is the PO", poIsOrder(openPOs, suppliers, "A", "o2") === true);
check("o1 arriving does not clear it", poIsOrder(openPOs, suppliers, "A", "o1") === false);
const after = updateOrder(suppliers, "s1", "o1", { receivedDate: today });
check("updateOrder touches only that order",
  after.s1.orders.find(o => o.id === "o1").receivedDate === today
  && after.s1.orders.find(o => o.id === "o2").receivedDate === null);

console.log("\nwhether an arrival adds to stock");
check("arriving today adds", arrivalAddsToStock({}, today) === true);
check("an old delivery with no count: follows the default (off for logging)",
  arrivalAddsToStock({}, dayOffset(-30)) === false);
check("... and on for an in-transit order marked arrived late",
  arrivalAddsToStock({}, dayOffset(-3), { pastDefault: true }) === true);
check("counted after it arrived: the count includes it",
  arrivalAddsToStock({ stockCountedAt: `${dayOffset(-1)}T15:00:00Z` }, dayOffset(-5), { pastDefault: true }) === false);
check("counted before it arrived: adds",
  arrivalAddsToStock({ stockCountedAt: `${dayOffset(-10)}T15:00:00Z` }, dayOffset(-5)) === true);
check("counted today, arriving today: adds", arrivalAddsToStock({ stockCountedAt: new Date().toISOString() }, today) === true);
check("no date: never adds", arrivalAddsToStock({}, "") === false);

console.log(failed ? `\n${failed} FAILED` : "\nAll order tests passed.");
if (failed) throw new Error(`${failed} failed`);
