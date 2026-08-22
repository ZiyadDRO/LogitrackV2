import { planningLeadTime, computeSkuLeadTimeStats, LEAD_TIME_MIN_DELIVERIES } from "./lib/helpers.js";
let pass = 0, fail = 0;
const ok = (n, c, d = "") => { c ? pass++ : fail++; console.log(`  [${c ? "PASS" : "FAIL"}] ${n}${c ? "" : " — " + d}`); };
const iso = (y, m, d) => new Date(Date.UTC(y, m - 1, d)).toISOString().slice(0, 10);
// deliveries: [orderedISO, leadDays]
const sup = (rows) => ({ S1: { name: "Acme", skuIds: ["A"], orders: rows.map(([o, L], i) => ({
  id: `o${i}`, skuId: "A", qty: 10, orderedDate: o,
  receivedDate: new Date(new Date(o).getTime() + L * 86400000).toISOString().slice(0, 10),
})) } });
const P = { leadTime: 21, coverage: 30 };

console.log("\n— baseline vs measured —");
ok("no deliveries → baseline", planningLeadTime("A", P, sup([])).source === "baseline");
ok("baseline value is your number", planningLeadTime("A", P, sup([])).days === 21);
const slow = sup([[iso(2026,1,5),20],[iso(2026,2,5),22],[iso(2026,3,5),24],[iso(2026,4,5),19]]);
const m = planningLeadTime("A", P, slow);
ok("enough deliveries → measured takes over", m.source === "measured", m.source);
ok("measured uses P80 of actual deliveries", m.days >= 22 && m.days <= 24, String(m.days));
ok("it reports how many it measured", m.n === 4, String(m.n));

console.log("\n— a PERMANENT change —");
// supplier switches to air freight in May; two fast deliveries since
const mixed = sup([[iso(2026,1,5),20],[iso(2026,2,5),22],[iso(2026,3,5),24],
                   [iso(2026,5,10),9],[iso(2026,6,10),10]]);
const changed = { ...P, leadTime: 11, leadTimeChangedAt: iso(2026,5,1) };
const c = planningLeadTime("A", changed, mixed);
ok("right after a change, old slow deliveries are excluded", c.source === "baseline", c.source);
ok("...so it plans to your NEW baseline, not the stale P80", c.days === 11, String(c.days));
ok("it says how many deliveries were set aside", c.supersededN === 3, String(c.supersededN));
ok("...and what the old measurement was", c.priorP80 != null && c.priorP80 >= 22, String(c.priorP80));
ok("it says how many more are needed", c.needed === 1, String(c.needed));
// one more fast delivery and it measures the NEW regime
const mixed2 = sup([[iso(2026,1,5),20],[iso(2026,2,5),22],[iso(2026,3,5),24],
                    [iso(2026,5,10),9],[iso(2026,6,10),10],[iso(2026,7,1),11]]);
const c2 = planningLeadTime("A", changed, mixed2);
ok("once 3 new deliveries land, it measures the NEW regime", c2.source === "measured", c2.source);
ok("...and the new P80 is fast, not dragged up by old data", c2.days <= 12, String(c2.days));

console.log("\n— reverting is lossless —");
const reverted = planningLeadTime("A", { ...P, leadTime: 21 }, mixed2);
ok("removing the change restores the FULL history", reverted.n === 6, String(reverted.n));
ok("...and the old, slower P80 comes back", reverted.days > c2.days, `${reverted.days} vs ${c2.days}`);
ok("nothing was deleted — every delivery is still counted",
   computeSkuLeadTimeStats("A", mixed2).n === 6);

console.log("\n— a ONE-OFF delay —");
const one = planningLeadTime("A", { ...changed, nextLeadTime: 40 }, mixed2);
ok("a one-off beats everything", one.days === 40 && one.source === "next-order");
ok("it does not disturb the changepoint", one.changedAt === changed.leadTimeChangedAt);
ok("clearing it returns to the measured value",
   planningLeadTime("A", changed, mixed2).days === c2.days);
ok("junk one-offs are ignored",
   planningLeadTime("A", { ...changed, nextLeadTime: -3 }, mixed2).source === "measured");

console.log("\n— manual override —");
ok("manual ignores history entirely",
   planningLeadTime("A", { ...P, leadTimeMode: "manual", leadTime: 30 }, mixed2).days === 30);
ok("but a one-off still wins over manual",
   planningLeadTime("A", { ...P, leadTimeMode: "manual", leadTime: 30, nextLeadTime: 45 }, mixed2).days === 45);

console.log("\n— gap checks —");
// supplier-level fallback: this product has 1 delivery, the supplier has plenty
const shared = { S1: { name: "Acme", skuIds: ["A", "B"], orders: [
  { id: "x1", skuId: "B", qty: 5, orderedDate: iso(2026,1,1), receivedDate: iso(2026,1,19) },
  { id: "x2", skuId: "B", qty: 5, orderedDate: iso(2026,2,1), receivedDate: iso(2026,2,20) },
  { id: "x3", skuId: "B", qty: 5, orderedDate: iso(2026,3,1), receivedDate: iso(2026,3,22) },
  { id: "x4", skuId: "A", qty: 5, orderedDate: iso(2026,4,1), receivedDate: iso(2026,4,19) },
] } };
const viaSup = planningLeadTime("A", P, shared);
ok("one own delivery → falls back to the supplier's record", viaSup.source === "supplier", viaSup.source);
ok("...using their P80, not your baseline", viaSup.days >= 18 && viaSup.days !== 21, String(viaSup.days));
ok("...and says how thin this product's own record is", viaSup.skuN === 1, String(viaSup.skuN));
ok("...naming the supplier", viaSup.supplierName === "Acme", String(viaSup.supplierName));
// once the product has its own record it stops borrowing
const own = { S1: { ...shared.S1, orders: [...shared.S1.orders,
  { id: "x5", skuId: "A", qty: 5, orderedDate: iso(2026,5,1), receivedDate: iso(2026,5,9) },
  { id: "x6", skuId: "A", qty: 5, orderedDate: iso(2026,6,1), receivedDate: iso(2026,6,10) }] } };
ok("3 own deliveries → stops borrowing from the supplier",
   planningLeadTime("A", P, own).source === "measured", planningLeadTime("A", P, own).source);
// a one-off must not leak into the backtest window
const withOneOff = { ...P, nextLeadTime: 40 };
ok("a one-off drives ORDERING", planningLeadTime("A", withOneOff, own).days === 40);
ok("...but NOT the backtested window",
   planningLeadTime("A", withOneOff, own, { ignoreOneOff: true }).days !== 40,
   String(planningLeadTime("A", withOneOff, own, { ignoreOneOff: true }).days));
ok("...which stays the steady-state value",
   planningLeadTime("A", withOneOff, own, { ignoreOneOff: true }).days ===
   planningLeadTime("A", P, own).days);
// no supplier at all
ok("no supplier linked → baseline", planningLeadTime("A", P, {}).source === "baseline");

const many = Array.from({ length: 200 }, () => planningLeadTime("A", changed, mixed2).days);
ok("deterministic", new Set(many).size === 1);
console.log(`\n=== ${pass} passed, ${fail} failed ===`);
process.exitCode = fail ? 1 : 0;
