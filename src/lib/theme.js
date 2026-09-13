// ─── TERMINAL THEME TOKENS ────────────────────────────────────────────────────
// One palette for the whole redesign. Every screen reads its colours from here so
// a change lands everywhere at once, and so dark and light stay in step — the two
// objects have exactly the same keys by construction.
//
// Colour carries meaning and nothing else: amber is the app's one accent and marks
// the thing you are being asked to act on; red/blue/green/over/dead are the five
// SKU statuses and are never used decoratively. Everything else is a grey.
//
// Figures are monospaced everywhere (`mono`), so a column of numbers lines up on
// the digit rather than drifting — that is most of why the design reads as it does.

export const MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace";
export const SANS = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

const DARK = {
  bg:      "#0a0b0d",   // page
  panel:   "#131519",   // raised surface
  sunken:  "#0d0f12",   // inset strip (table headers, footers)
  ink:     "#f0f1f3",   // primary text
  soft:    "#b3b8c0",   // secondary text  — was #8b8f96, ~3.9:1, too dim to read
  dim:     "#969ba3",   // captions        — was #6c6c74
  faint:   "#7c828a",   // tertiary        — was #5c5c64, below AA at any size
  line:    "rgba(255,255,255,.18)",   // was .11 — a 2px rule at 11% is still a rule you infer
  line2:   "rgba(255,255,255,.34)",   // was .22
  amber:   "#f0bb5c",
  amberSoft: "rgba(232,176,74,.10)",
  green:   "#4ec767",
  blue:    "#6cb2ff",
  red:     "#f85149",
  over:    "#d29922",
  dead:    "#5c5c64",
  redBg:   "rgba(248,81,73,.14)",   redFg:   "#f8837c",
  blueBg:  "rgba(88,166,255,.14)",  blueFg:  "#79b8ff",
  overBg:  "rgba(210,153,34,.14)",  overFg:  "#dfb457",
  deadBg:  "rgba(139,143,150,.12)", deadFg:  "#8b8f96",
  greenBg: "rgba(63,185,80,.13)",   greenFg: "#7fc9a0",
  btnBg:   "#e8e8ea",  btnFg: "#0a0b0d",
  onFill:  "#0a0b0d",               // text laid on a saturated status fill
  shadow:  "none",
};

const LIGHT = {
  bg:      "#f4f4f2",
  panel:   "#ffffff",
  sunken:  "#faf9f6",
  ink:     "#141517",
  soft:    "#44484f",   // was #5c6069
  dim:     "#5a5f66",   // was #767a82
  faint:   "#70757c",   // was #9aa0a8, ~2.6:1 on white
  line:    "rgba(10,11,13,.21)",      // was .13
  line2:   "rgba(10,11,13,.38)",      // was .26
  amber:   "#a97410",
  amberSoft: "rgba(169,116,16,.09)",
  green:   "#1a7f37",
  blue:    "#0969da",
  red:     "#cf222e",
  over:    "#9a6700",
  dead:    "#8c8f96",
  redBg:   "rgba(207,34,46,.10)",   redFg:   "#a40e26",
  blueBg:  "rgba(9,105,218,.10)",   blueFg:  "#0a58ca",
  overBg:  "rgba(154,103,0,.12)",   overFg:  "#7a5200",
  deadBg:  "rgba(90,95,104,.10)",   deadFg:  "#5c6069",
  greenBg: "rgba(26,127,55,.11)",   greenFg: "#116329",
  btnBg:   "#17181a",  btnFg: "#f4f4f2",
  onFill:  "#ffffff",
  shadow:  "0 1px 2px rgba(15,23,42,.05)",
};

export const terminal = lm => (lm ? LIGHT : DARK);

/* MODAL BACKDROP — one definition, so every popup dims the page the same way.
 *
 * A flat black wash at 50% left the page perfectly legible underneath: you could
 * still read the row of figures behind a form asking you to type prices into it,
 * and the eye kept going back to them. Blur is what actually removes the competing
 * content — the colours stay, so you can see WHAT is behind and that you have not
 * navigated away, but there is nothing left to read.
 *
 * The wash is lighter in light mode. A 62% black over a near-white page turns it to
 * mud; over the near-black dark page the same value reads as a gentle dim.
 */
export const scrim = lm => ({
  position: "absolute", inset: 0,
  background: lm ? "rgba(24,26,30,.34)" : "rgba(0,0,0,.62)",
  backdropFilter: "blur(10px) saturate(.85)",
  WebkitBackdropFilter: "blur(10px) saturate(.85)",
});

/* TYPE SCALE — the floor is 12.5px and it is not negotiable.
 *
 * The first cut of this design ran captions at 9.5px in a 40%-contrast grey. On a
 * designer's laptop that reads as precision; to a 60-year-old owner running his own
 * purchasing it reads as ants. Presbyopia is near-universal past 45 and costs about
 * a third of your effective acuity at screen distance, so anything that has to be
 * READ — as opposed to glanced at — starts at 13px here.
 *
 * Only `tick` goes below that, and only for chart axis marks, which are landmarks
 * rather than text: you locate them, you do not read them.
 *
 * Every step moved up about one and a half pixels — roughly 11% — in one pass, so the
 * RELATIONSHIPS between steps are unchanged and no screen drifts away from the others.
 * The same shift was applied to the Tailwind `text-[Npx]` literals scattered through
 * the JSX, which are this same scale written a second way. If you add a size here, add
 * it there too, or the two halves of the app start disagreeing.
 *
 * 11% and not 25%: the layouts are almost entirely fr-units and `minWidth:0`, so they
 * absorb a modest bump, but the handful of FIXED pixel columns (the supplier method
 * tables) had to be widened by hand to match. A bigger jump would have meant re-cutting
 * every one of those, with a clipped header anywhere one was missed.
 */
export const fs = {
  tick:  12.5,   // chart axis labels only
  cap:   13,     // COLUMN HEADERS / SECTION CAPTIONS (uppercase, so effectively larger)
  small: 14,     // secondary lines, footnotes, provenance
  body:  15,     // prose, descriptions
  row:   15.5,   // table rows and list items — the workhorse
  num:   16.5,   // figures inside tables
  lead:  18.5,   // panel titles
  big:   32,     // headline figures
  huge:  38,
};

/* Monospace is for FIGURES, not for prose. A column of numbers must line up on the
 * digit; a sentence set in mono is measurably slower to read, and the first cut set
 * whole paragraphs in it. Rule: numbers, IDs, dates and codes get `mono`; anything
 * with a verb in it gets the sans. */

// Hatching says "modelled", in texture rather than in shade — a paler fill would
// only read as "less", which is not what a projected repeat order is.
export const projFill = T =>
  `repeating-linear-gradient(135deg, ${T.amber}66 0 5px, ${T.amber}1f 5px 10px)`;

// The same texture at chip scale, where stripes would otherwise cut across the digits.
// Laid over the panel colour so the number keeps its contrast in both themes.
export const projFillSoft = T =>
  `repeating-linear-gradient(135deg, ${T.amber}33 0 5px, ${T.amber}0d 5px 10px), ${T.panel}`;

// ─── shared primitives ────────────────────────────────────────────────────────
export const panelStyle = T => ({
  background: T.panel, border: `1px solid ${T.line}`, boxShadow: T.shadow,
});
export const capStyle = T => ({
  fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase",
  color: T.dim, fontWeight: 600,
});
export const monoStyle = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
