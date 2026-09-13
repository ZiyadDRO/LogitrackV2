import React, { useState, useEffect, useCallback } from 'react';
import { terminal, fs, MONO, SANS } from '../lib/theme';

/**
 * Live accuracy — one tile per week.
 *
 * The backtest re-runs the models over history and grades them. That's the right tool at
 * setup, but it can only ever re-simulate: every window it scores is one where the answer
 * already existed. This shows the other thing — what the tool said on a given Sunday, and
 * what really sold in the seven days that followed.
 *
 * The layout is a grid of week tiles under big month headings, because that's how anyone
 * actually navigates a year of results. Weeks don't divide into months, so each week is
 * filed under the month holding MOST of its days — every week lands in exactly one place,
 * always the month it mostly happened in.
 *
 * Two properties are load-bearing and enforced server-side, not here:
 *   - a week cannot be graded before it has fully elapsed;
 *   - a sealed forecast is never silently rewritten. A promotion declared mid-week
 *     produces a visible REVISION that keeps the original figure alongside it.
 *
 * ── ON NOT MAKING A GOOD WEEK LOOK LIKE A BAD ONE ───────────────────────────
 *
 * A week that went 28-for-30 is a good week. Rendered as a percentage alone it is a
 * number you have to interpret; rendered as a list of the two that missed it is a
 * list of failures. Absence of good news is not neutral — the page has to carry the
 * hits with the same weight reality gives them.
 *
 * So every tile now has a TALLY BAR: one block per tracked product, filled for the
 * ones that landed inside their band. The shape of the week reads before any figure
 * does, and an overwhelmingly-full bar is the honest first impression. The expanded
 * view lists every product, not just the misses, and a miss is described as a band
 * that was too narrow rather than as a product that went wrong — which is also the
 * more accurate statement, because widening the band IS the fix.
 */

const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];

const fmtDay = (iso) => {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
};
const monthTitle = (ym) => {
  const [y, m] = String(ym).split("-").map(Number);
  return `${MONTHS[(m || 1) - 1]} ${y}`;
};

export default function LiveAccuracy({ api, lm = false, onPickSku = null }) {
  const [data, setData]   = useState(null);
  const [busy, setBusy]   = useState(null);
  const [err, setErr]     = useState(null);
  const [open, setOpen]   = useState(null);      // week key whose detail is expanded
  const [confirmReset, setConfirmReset] = useState(false);

  const T = terminal(lm);
  const mono  = { fontFamily: MONO, fontVariantNumeric: "tabular-nums" };
  const cap   = { fontSize: fs.cap, letterSpacing: ".07em", textTransform: "uppercase", color: T.dim, fontWeight: 600 };
  const panel = { background: T.panel, border: `2px solid ${T.line}`, boxShadow: T.shadow };
  const btn   = { ...mono, fontSize: fs.body, fontWeight: 500, padding: "7px 12px", borderRadius: 2,
                  background: "transparent", color: T.soft, border: `2px solid ${T.line2}`,
                  whiteSpace: "nowrap", cursor: "pointer" };

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${api}/api/livelog`);
      setData(await r.json());
      setErr(null);
    } catch { setErr("Couldn't reach the engine."); }
  }, [api]);

  /* Runs itself: opens this week's forecasts if they aren't open yet, and grades anything
     whose week has fully elapsed. Both are idempotent, so calling it constantly is safe. */
  const tick = useCallback(async () => {
    try {
      const r = await fetch(`${api}/api/livelog/tick`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const d = await r.json();
      await load();
      if (d?.score?.error) setErr(`Couldn't read the store — nothing was graded, and every week is still queued. (${d.score.error})`);
      else if (d?.score?.sourceFailed) setErr("Couldn't read the store — nothing was graded, and every week is still queued.");
    } catch { setErr("Couldn't reach the engine."); }
  }, [api, load]);

  useEffect(() => {
    tick();
    const t = setInterval(tick, 60 * 60 * 1000);
    return () => clearInterval(t);
  }, [tick]);

  const reset = async () => {
    setBusy("reset");
    try {
      await fetch(`${api}/api/livelog/reset`, { method: "POST" });
      setOpen(null);
      await load();
    } catch { setErr("Couldn't reach the engine."); }
    setBusy(null); setConfirmReset(false);
  };

  if (!data) {
    return (
      <div style={{ ...panel, padding: 20, fontFamily: SANS, color: T.ink }}>
        <div style={{ fontSize: 17, fontWeight: 600 }}>Live accuracy</div>
        <div style={{ fontSize: fs.body, color: T.soft, marginTop: 5 }}>{err || "Loading…"}</div>
      </div>
    );
  }

  const s = data.summary || {};
  const cal = s.calibration || {};
  const acc = s.accuracy || {};
  const weeks = data.weeks || [];
  const band = weeks[0]?.band ?? 80;

  /* Colour carries the verdict, so it has to be legible rather than decorative. Grey is
     reserved for "nothing measured yet" — anything with a result gets a real colour. */
  const TONE = {
    good:    { line: T.green, soft: `${T.green}16`, tx: T.green },
    ok:      { line: T.blue,  soft: `${T.blue}14`,  tx: T.blue },
    poor:    { line: T.red,   soft: `${T.red}14`,   tx: T.red },
    waiting: { line: T.amber, soft: `${T.amber}12`, tx: T.amber },
    idle:    { line: T.line2, soft: "transparent",  tx: T.soft },
  };

  const toneFor = (w) => {
    if (!w.scored) return w.due ? TONE.waiting : TONE.idle;
    const hit = w.hitRate;
    if (hit == null) return TONE.ok;
    if (hit >= band - 8) return TONE.good;
    if (hit >= band - 20) return TONE.ok;
    return TONE.poor;
  };

  // Month groupings, newest first, preserving the server's week ordering.
  const byMonth = [];
  for (const w of weeks) {
    const last = byMonth[byMonth.length - 1];
    if (last && last.month === w.month) last.weeks.push(w);
    else byMonth.push({ month: w.month, weeks: [w] });
  }

  /* The honest first impression: one block per graded product, filled for the ones
     that landed inside their band. Capped so a 200-product catalogue still renders a
     bar rather than a smear — the shape is the point, not the individual blocks. */
  const Tally = ({ inBand, total, tone }) => {
    if (!total) return null;
    const MAX = 30;
    const n = Math.min(total, MAX);
    const filled = Math.round((inBand / total) * n);
    return (
      <div title={`${inBand} of ${total} forecasts landed inside their band`}
        /* Capped: stretched across an expanded tile the blocks stop reading as one
           per product and start reading as a progress bar, which is a different and
           wrong claim. */
        style={{ display: "flex", gap: 2, marginTop: 10, maxWidth: 360 }}>
        {Array.from({ length: n }, (_, i) => (
          <div key={i} style={{ flex: 1, height: 10, minWidth: 3,
            background: i < filled ? tone.tx : T.line2, opacity: i < filled ? 1 : .8 }} />
        ))}
      </div>
    );
  };

  const Head = ({ label, value, sub, tone }) => (
    <div style={{ ...panel, padding: "13px 16px",
      borderLeft: tone ? `2px solid ${tone.line}` : `2px solid ${T.line}`,
      background: tone ? tone.soft : T.panel }}>
      <div style={cap}>{label}</div>
      <div style={{ ...mono, fontSize: 26, fontWeight: 600, lineHeight: 1.1, marginTop: 6,
        color: tone ? tone.tx : T.ink }}>{value}</div>
      {sub && <div style={{ fontSize: fs.small, color: T.soft, marginTop: 5 }}>{sub}</div>}
    </div>
  );

  const calTone = cal.verdict === "calibrated" ? TONE.good
    : cal.verdict === "overconfident" ? TONE.poor
    : cal.verdict === "underconfident" ? TONE.ok : TONE.idle;
  const calLabel = { calibrated: "Calibrated", overconfident: "Over-confident",
                     underconfident: "Cautious", "too-few": "Not enough yet",
                     "no-data": "Nothing graded" }[cal.verdict] || "Nothing graded";

  /* The headline the page should lead with. A percentage asks to be interpreted;
     "137 of 150 forecasts landed inside their band" does not. */
  const totals = weeks.reduce((a, w) => w.scored
    ? { inBand: a.inBand + (w.inBand || 0), scored: a.scored + (w.scored || 0) } : a,
    { inBand: 0, scored: 0 });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, fontFamily: SANS, color: T.ink }}>
      <style>{`.la-row:hover{background:${T.sunken};} .la-tile:hover{border-color:${T.line2};}`}</style>

      {/* ── header ─────────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 280 }}>
          <h2 style={{ fontSize: 26, fontWeight: 700, letterSpacing: "-.03em", margin: 0 }}>Live accuracy</h2>
          {totals.scored > 0 && (
            <div style={{ fontSize: 15, color: T.soft, marginTop: 6 }}>
              <b style={{ ...mono, color: T.green, fontWeight: 600 }}>{totals.inBand}</b> of{" "}
              <b style={{ ...mono, color: T.ink, fontWeight: 600 }}>{totals.scored}</b> forecasts landed inside their band.
            </div>
          )}
          <p style={{ fontSize: fs.body, color: T.soft, marginTop: 6, maxWidth: 680, lineHeight: 1.6 }}>
            One tile per week. Each Sunday the tool seals what it expects every product to sell over the
            following seven days, then grades itself once that week is over. Unlike the backtest, none of
            these forecasts could see what came next.
          </p>
        </div>
        <button disabled={busy} onClick={async () => { setBusy("tick"); await tick(); setBusy(null); }}
          style={{ ...btn, opacity: busy ? .6 : 1 }}>{busy === "tick" ? "Checking…" : "Check now"}</button>
        {confirmReset ? (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button disabled={busy} onClick={reset}
              style={{ ...btn, background: T.red, color: T.onFill, border: "2px solid transparent", fontWeight: 600 }}>
              {busy === "reset" ? "Clearing…" : "Delete all weeks"}
            </button>
            <button onClick={() => setConfirmReset(false)}
              style={{ ...btn, border: "none", color: T.faint }}>cancel</button>
          </div>
        ) : (
          <button onClick={() => setConfirmReset(true)} style={btn}>Reset</button>
        )}
      </div>

      {err && (
        <div style={{ border: `2px solid ${T.red}55`, background: `${T.red}14`, color: T.red,
          padding: "10px 14px", fontSize: fs.body }}>{err}</div>
      )}

      {/* ── the numbers that matter ────────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))", gap: 12 }}>
        <Head label="Calibration" tone={calTone}
          value={cal.hitRate != null ? `${cal.hitRate}%` : "—"}
          sub={cal.hitRate != null ? `${calLabel} · target ${cal.nominal}%` : calLabel} />
        <Head label="Typical miss" value={acc.wape != null ? `${acc.wape}%` : "—"} sub="weighted, vs actual" />
        <Head label="Bias" value={acc.bias != null ? `${acc.bias > 0 ? "+" : ""}${acc.bias}%` : "—"}
          sub={acc.bias == null ? "nothing graded yet"
            : acc.bias > 2 ? "forecasting high" : acc.bias < -2 ? "forecasting low" : "balanced"} />
        <Head label="Weeks graded" value={`${s.scored ?? 0}`}
          sub={s.nextDueAt ? `next in ${s.nextDueInDays}d` : `${s.waiting ?? 0} still running`} />
      </div>

      {cal.verdict === "overconfident" && (
        <div style={{ border: `2px solid ${T.red}55`, background: `${T.red}12`, color: T.red,
          padding: "11px 14px", fontSize: fs.body, lineHeight: 1.6 }}>
          <b>The bands are too narrow.</b> Reality lands outside the {cal.nominal}% band more often than it
          should, so buffers sized from these intervals are thin and your real service level is below target.
        </div>
      )}

      {/* ── the grid ───────────────────────────────────────────────────────── */}
      {weeks.length === 0 ? (
        <div style={{ ...panel, padding: "40px 24px", textAlign: "center" }}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Waiting for the first week to close</div>
          <div style={{ fontSize: fs.body, color: T.soft, marginTop: 7, maxWidth: 460, marginLeft: "auto",
            marginRight: "auto", lineHeight: 1.6 }}>
            Nothing to do — this runs on its own. A tile appears for the current week straight away, and
            fills in with a result once those seven days have actually happened.
          </div>
        </div>
      ) : byMonth.map(({ month, weeks: ws }) => (
        <div key={month}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 14, marginBottom: 12 }}>
            <h3 style={{ fontSize: 17, fontWeight: 600, letterSpacing: "-.02em", margin: 0 }}>{monthTitle(month)}</h3>
            <div style={{ flex: 1, borderBottom: `2px solid ${T.line}` }} />
            <span style={{ fontSize: fs.small, color: T.soft }}>{ws.length} week{ws.length === 1 ? "" : "s"}</span>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(268px,1fr))", gap: 12 }}>
            {ws.map((w) => {
              const t = toneFor(w);
              const isOpen = open === w.week;
              const entries = (data.entries || []).filter(e => e.origin === w.week);
              return (
                <div key={w.week} className="la-tile"
                  style={{ background: T.panel, border: `2px solid ${T.line}`, borderTop: `2px solid ${t.line}`,
                    overflow: "hidden", gridColumn: isOpen ? "1 / -1" : "auto" }}>
                  <button onClick={() => setOpen(isOpen ? null : w.week)}
                    style={{ width: "100%", textAlign: "left", padding: "13px 16px", background: "transparent",
                      border: "none", cursor: "pointer", color: T.ink, fontFamily: SANS }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                      <span style={{ ...mono, fontSize: fs.row, fontWeight: 600, color: T.ink }}>
                        {fmtDay(w.week)} – {fmtDay(w.endsOn)}
                      </span>
                      {w.amended > 0 && (
                        <span title="A forecast in this week was revised mid-week after a promotion was declared. Both figures are kept."
                          style={{ ...mono, fontSize: fs.tick, padding: "2px 6px", background: T.sunken,
                            border: `2px solid ${T.line}`, color: T.soft }}>revised</span>
                      )}
                      <span style={{ ...mono, marginLeft: "auto", fontSize: fs.small, color: T.faint }}>
                        {isOpen ? "▾" : "▸"}
                      </span>
                    </div>

                    {w.scored ? (
                      <>
                        <div style={{ display: "flex", alignItems: "baseline", gap: 9, marginTop: 10 }}>
                          <span style={{ ...mono, fontSize: 30, fontWeight: 600, lineHeight: 1, color: t.tx }}>
                            {w.hitRate != null ? `${w.hitRate}%` : "—"}
                          </span>
                          <span style={{ fontSize: fs.body, color: T.soft }}>
                            {w.inBand} of {w.scored} in band
                          </span>
                        </div>
                        <Tally inBand={w.inBand} total={w.scored} tone={t} />
                        <div style={{ ...mono, fontSize: fs.small, marginTop: 9, color: T.soft }}>
                          said {Math.round(w.predicted)} · sold {Math.round(w.actual ?? 0)} units
                        </div>
                      </>
                    ) : (
                      <>
                        <div style={{ fontSize: 17, fontWeight: 600, marginTop: 10, color: t.tx }}>
                          {w.due ? "Ready to grade" : "In progress"}
                        </div>
                        <div style={{ fontSize: fs.body, marginTop: 5, color: T.soft }}>
                          {w.products} product{w.products === 1 ? "" : "s"} · {Math.round(w.predicted)} units expected
                        </div>
                        <div style={{ ...mono, fontSize: fs.small, marginTop: 7, color: T.faint }}>
                          grades {fmtDay(w.gradableFrom)}
                        </div>
                      </>
                    )}
                  </button>

                  {isOpen && (
                    <div style={{ borderTop: `2px solid ${T.line}`, background: T.sunken }}>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 150px 90px 130px", gap: "0 16px",
                        padding: "9px 16px", ...cap, borderBottom: `2px solid ${T.line}` }}>
                        <span>Product</span><span style={{ textAlign: "right" }}>Expected</span>
                        <span style={{ textAlign: "right" }}>Sold</span><span style={{ textAlign: "right" }}>Result</span>
                      </div>
                      {entries.map((e) => (
                        <div key={e.id} className="la-row"
                          onClick={() => onPickSku && onPickSku(e.sku)}
                          style={{ display: "grid", gridTemplateColumns: "1fr 150px 90px 130px", gap: "0 16px",
                            alignItems: "baseline", padding: "8px 16px", fontSize: fs.row,
                            borderTop: `2px solid ${T.line}`, cursor: onPickSku ? "pointer" : "default" }}>
                          <span title={e.skuName} style={{ color: T.ink, whiteSpace: "nowrap",
                            overflow: "hidden", textOverflow: "ellipsis" }}>
                            {e.skuName}
                            {e.amended && <span style={{ ...mono, fontSize: fs.small, color: T.faint }}> revised from {e.originalPredicted}</span>}
                          </span>
                          <span style={{ ...mono, textAlign: "right", color: T.soft }}>
                            {Math.round(e.predicted)}
                            {e.lo != null && <span style={{ color: T.faint }}> ({Math.round(e.lo)}–{Math.round(e.hi)})</span>}
                          </span>
                          <span style={{ ...mono, textAlign: "right", color: e.scoredAt ? T.ink : T.faint }}>
                            {e.scoredAt ? Math.round(e.actual) : "—"}
                          </span>
                          {/* The verdict belongs to the BAND, not the product. A product that
                              sold more than expected did not fail; the range it was given was
                              too narrow — and widening it is literally the fix. */}
                          <span style={{ ...mono, textAlign: "right", fontSize: fs.small, fontWeight: 600,
                            color: !e.scoredAt ? T.faint : e.inBand === false ? T.over : T.green }}>
                            {!e.scoredAt ? (e.isDue ? "ready" : `${e.dueInDays}d`)
                              : e.inBand === false ? "band too narrow" : "in band"}
                          </span>
                        </div>
                      ))}
                      <div style={{ padding: "10px 16px", fontSize: fs.small, color: T.soft,
                        borderTop: `2px solid ${T.line}`, lineHeight: 1.6 }}>
                        Expected shows the forecast and its {w.band}% range. A week counts as accurate when what
                        actually sold lands inside that range.
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}

      <p style={{ fontSize: fs.small, color: T.soft, lineHeight: 1.65, margin: 0 }}>
        Weeks run Sunday to Saturday and are filed under the month holding most of their days. Forecasts are
        sealed when made; if a promotion is declared mid-week the tile is marked <b>revised</b> and
        keeps both figures, so a campaign is never mistaken for a modelling win.
      </p>
    </div>
  );
}
