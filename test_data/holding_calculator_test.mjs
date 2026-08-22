import { computeHolding, classifyIndustryLocally, defaultsForIndustry, INDUSTRY } from "./lib/holding.js";

let pass = 0, fail = 0;
const ok = (name, cond, detail = "") => { cond ? pass++ : fail++; console.log(`  [${cond ? "PASS" : "FAIL"}] ${name}${cond ? "" : " — " + detail}`); };

// 1. THE headline claim: same answers -> same number, every time.
const answers = { capital: "bank_loan", storageBulk: "own_bulky", obsolescence: "seasonal", risk: "fragile" };
const runs = Array.from({ length: 500 }, () => computeHolding(answers).holding);
ok("500 identical runs give one identical rate", new Set(runs).size === 1, `saw ${[...new Set(runs)]}`);
ok("the rate is a whole number (no drifting decimals)", Number.isInteger(runs[0]), String(runs[0]));

// 2. Every combination is stable and sane.
let unstable = 0, outOfRange = 0, n = 0;
for (const c of Object.keys((await import("./lib/holding.js")).CAPITAL))
  for (const s of ["own_small", "own_bulky", "third_small", "third_bulky"])
    for (const o of ["stable", "seasonal", "trend", "tech", "perishable"])
      for (const r of ["durable", "fragile", "valuable"]) {
        const a = { capital: c, storageBulk: s, obsolescence: o, risk: r };
        const x = computeHolding(a).holding, y = computeHolding(a).holding;
        n++; if (x !== y) unstable++;
        if (x < 5 || x > 60) outOfRange++;
      }
ok(`all ${n} answer combinations are stable`, unstable === 0, `${unstable} unstable`);
ok("all combinations land in a believable 5–60% band", outOfRange === 0, `${outOfRange} outside`);

// 3. The breakdown must add up to the headline.
const { holding, breakdown } = computeHolding(answers);
ok("breakdown sums to the headline rate", breakdown.reduce((t, b) => t + b.pct, 0) === holding,
   `${breakdown.reduce((t, b) => t + b.pct, 0)} vs ${holding}`);

// 4. A stated borrowing rate is used verbatim, not bracketed.
ok("a known rate replaces the capital bracket",
   computeHolding({ ...answers, knownRate: 9 }).breakdown[0].pct === 9);
ok("an absurd known rate is clamped, not trusted",
   computeHolding({ ...answers, knownRate: 999 }).breakdown[0].pct === 60);

// 5. Direction sanity — each lever moves the right way.
const base = computeHolding(answers).holding;
ok("expensive financing raises it", computeHolding({ ...answers, capital: "expensive" }).holding > base);
ok("dropshipping lowers it (no storage)", computeHolding({ ...answers, dropship: true }).holding < base);
ok("perishable raises it vs stable",
   computeHolding({ ...answers, obsolescence: "perishable" }).holding >
   computeHolding({ ...answers, obsolescence: "stable" }).holding);

// 6. Local keyword classification is deterministic and covers the obvious cases.
const cases = [["we sell bathroom vanities online", "furniture"], ["LED mirrors and rugs", "home_decor"],
               ["brass faucets and tiles", "building"], ["running shoes", "apparel"],
               ["fresh bakery goods", "food"], ["gold necklaces", "jewellery"]];
let miss = 0;
for (const [txt, want] of cases) {
  const a = classifyIndustryLocally(txt), b = classifyIndustryLocally(txt);
  if (a !== want || a !== b) { miss++; console.log(`     ${txt} -> ${a} (wanted ${want})`); }
}
ok("keyword classification is correct and repeatable", miss === 0, `${miss} wrong`);
ok("unknown text falls through to no match", classifyIndustryLocally("zzzz qqqq") === null);

// 7. Industry presets are complete.
const bad = Object.keys(INDUSTRY).filter((k) => {
  const d = defaultsForIndustry(k);
  return !d.storageBulk || !d.obsolescence || !d.risk;
});
ok("every industry preset is complete", bad.length === 0, String(bad));
console.log(`\n=== ${pass} passed, ${fail} failed ===`);
process.exitCode = fail ? 1 : 0;
