// The holding-cost wizard's local industry matcher reads whole words (backlog batch 12).
// Run:  node tests/holding.test.mjs
import { classifyIndustryLocally as c } from "../src/lib/holding.js";

let failed = 0;
const check = (name, got, want) => {
  const ok = got === want;
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : ` — got ${got}, want ${want}`}`);
  if (!ok) failed += 1;
};

check('"auto parts" is auto parts, not art', c("We sell auto parts"), "auto_parts");
check('"spare parts for boats" is not home decor', c("spare parts for boats"), null);
check('"wall art" is home decor', c("wall art and prints"), "home_decor");
check('"tealights" is not tea', c("tealights"), null);
check('"green tea" is food', c("green tea"), "food");
check('"furniture" still matches its stem', c("Bathroom furniture"), "furniture");
check('"vanities" still matches its stem', c("bathroom vanities"), "furniture");
check('plurals: "rugs"', c("handmade rugs"), "home_decor");
check('"catalog" is not a cat', c("our catalog of widgets"), null);
check('"cat toys" is a toy shop first', c("cat toys"), "toys");
check("empty text", c(""), null);

console.log(failed ? `\n${failed} FAILED` : "\nAll holding matcher tests passed.");
if (failed) throw new Error(`${failed} failed`);
