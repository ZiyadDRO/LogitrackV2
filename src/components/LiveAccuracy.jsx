import React, { useState, useEffect, useCallback, useRef } from 'react';

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

  const card   = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const text   = lm ? "text-slate-900" : "text-slate-100";
  const muted  = lm ? "text-slate-600" : "text-slate-400";
  const faint  = lm ? "text-slate-500" : "text-slate-500";
  const divide = lm ? "border-slate-200" : "border-slate-700/40";
  const btn    = `text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border transition-all ${
    lm ? "bg-white border-slate-300 text-slate-700 hover:border-violet-400"
       : "bg-slate-800 border-slate-700/60 text-slate-200 hover:border-violet-600"}`;

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
      <div className={`${card} border rounded-2xl p-5`}>
        <div className={`text-base font-bold ${text}`}>Live accuracy</div>
        <div className={`text-xs mt-1 ${muted}`}>{err || "Loading…"}</div>
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
    good:    lm ? { bg: "bg-emerald-50", br: "border-emerald-300", tx: "text-emerald-800", dot: "bg-emerald-500" }
                : { bg: "bg-emerald-500/10", br: "border-emerald-600/50", tx: "text-emerald-300", dot: "bg-emerald-400" },
    ok:      lm ? { bg: "bg-sky-50", br: "border-sky-300", tx: "text-sky-800", dot: "bg-sky-500" }
                : { bg: "bg-sky-500/10", br: "border-sky-600/50", tx: "text-sky-300", dot: "bg-sky-400" },
    poor:    lm ? { bg: "bg-rose-50", br: "border-rose-300", tx: "text-rose-800", dot: "bg-rose-500" }
                : { bg: "bg-rose-500/10", br: "border-rose-600/50", tx: "text-rose-300", dot: "bg-rose-400" },
    waiting: lm ? { bg: "bg-violet-50", br: "border-violet-300", tx: "text-violet-800", dot: "bg-violet-500" }
                : { bg: "bg-violet-500/10", br: "border-violet-600/50", tx: "text-violet-300", dot: "bg-violet-400" },
    idle:    lm ? { bg: "bg-slate-50", br: "border-slate-200", tx: "text-slate-600", dot: "bg-slate-400" }
                : { bg: "bg-slate-800/40", br: "border-slate-700", tx: "text-slate-400", dot: "bg-slate-600" },
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

  const Head = ({ label, value, sub, tone }) => (
    <div className={`rounded-xl border px-3.5 py-2.5 ${tone ? `${tone.bg} ${tone.br}` : `${card}`}`}>
      <div className={`text-[10px] uppercase tracking-wider font-bold ${tone ? tone.tx : faint} opacity-80`}>{label}</div>
      <div className={`text-xl font-bold tabular-nums leading-tight ${tone ? tone.tx : text}`}>{value}</div>
      {sub && <div className={`text-[11px] ${tone ? tone.tx : faint} opacity-80`}>{sub}</div>}
    </div>
  );

  const calTone = cal.verdict === "calibrated" ? TONE.good
    : cal.verdict === "overconfident" ? TONE.poor
    : cal.verdict === "underconfident" ? TONE.ok : TONE.idle;
  const calLabel = { calibrated: "Calibrated", overconfident: "Over-confident",
                     underconfident: "Cautious", "too-few": "Not enough yet",
                     "no-data": "Nothing graded" }[cal.verdict] || "Nothing graded";

  return (
    <div className="space-y-5">
      {/* ── header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-start gap-3 flex-wrap">
        <div className="flex-1 min-w-[260px]">
          <h2 className={`text-lg font-bold tracking-tight ${text}`}>Live accuracy</h2>
          <p className={`text-xs mt-1 max-w-2xl ${muted}`}>
            One tile per week. Each Sunday the tool seals what it expects every product to sell over the
            following seven days, then grades itself once that week is over. Unlike the backtest, none of
            these forecasts could see what came next.
          </p>
        </div>
        <button disabled={busy} onClick={async () => { setBusy("tick"); await tick(); setBusy(null); }}
          className={btn}>{busy === "tick" ? "Checking…" : "Check now"}</button>
        {confirmReset ? (
          <div className="flex items-center gap-1.5">
            <button disabled={busy} onClick={reset}
              className={`text-[11px] font-bold px-2.5 py-1.5 rounded-lg text-white transition-colors ${lm ? "bg-rose-600 hover:bg-rose-500" : "bg-rose-700 hover:bg-rose-600"}`}>
              {busy === "reset" ? "Clearing…" : "Delete all weeks"}
            </button>
            <button onClick={() => setConfirmReset(false)} className={`text-[11px] ${faint}`}>cancel</button>
          </div>
        ) : (
          <button onClick={() => setConfirmReset(true)}
            className={`text-[11px] px-2.5 py-1.5 rounded-lg border transition-all ${lm ? "border-slate-300 text-slate-600 hover:border-rose-400 hover:text-rose-700" : "border-slate-700/60 text-slate-400 hover:border-rose-800 hover:text-rose-300"}`}>
            Reset
          </button>
        )}
      </div>

      {err && (
        <div className={`rounded-xl border px-3.5 py-2.5 text-xs ${lm ? "bg-rose-50 border-rose-200 text-rose-800" : "bg-rose-500/10 border-rose-700/50 text-rose-300"}`}>{err}</div>
      )}

      {/* ── the numbers that matter ────────────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
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
        <div className={`rounded-xl border px-3.5 py-2.5 text-xs ${lm ? "bg-rose-50 border-rose-200 text-rose-800" : "bg-rose-500/10 border-rose-700/50 text-rose-300"}`}>
          Reality lands outside the {cal.nominal}% band more often than it should. Buffers sized from these
          intervals are too thin, so your real service level is below target.
        </div>
      )}

      {/* ── the grid ───────────────────────────────────────────────────────── */}
      {weeks.length === 0 ? (
        <div className={`${card} border rounded-2xl px-5 py-10 text-center`}>
          <div className={`text-sm font-bold ${text}`}>Waiting for the first week to close</div>
          <div className={`text-xs mt-1.5 max-w-md mx-auto ${muted}`}>
            Nothing to do — this runs on its own. A tile appears for the current week straight away, and
            fills in with a result once those seven days have actually happened.
          </div>
        </div>
      ) : byMonth.map(({ month, weeks: ws }) => (
        <div key={month}>
          <div className="flex items-baseline gap-3 mb-2.5">
            <h3 className={`text-base font-bold tracking-tight ${text}`}>{monthTitle(month)}</h3>
            <div className={`flex-1 border-b ${divide}`} />
            <span className={`text-[11px] ${faint}`}>{ws.length} week{ws.length === 1 ? "" : "s"}</span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
            {ws.map((w) => {
              const t = toneFor(w);
              const isOpen = open === w.week;
              const entries = (data.entries || []).filter(e => e.origin === w.week);
              return (
                <div key={w.week} className={`rounded-2xl border overflow-hidden transition-all ${t.bg} ${t.br} ${isOpen ? "col-span-full" : ""}`}>
                  <button onClick={() => setOpen(isOpen ? null : w.week)}
                    className="w-full text-left px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${t.dot}`} />
                      <span className={`text-[13px] font-bold ${t.tx}`}>
                        {fmtDay(w.week)} – {fmtDay(w.endsOn)}
                      </span>
                      {w.amended > 0 && (
                        <span className={`text-[9px] px-1.5 py-0.5 rounded ${lm ? "bg-white/70" : "bg-black/20"} ${t.tx}`}>revised</span>
                      )}
                    </div>

                    {w.scored ? (
                      <>
                        <div className={`mt-2.5 text-3xl font-bold tabular-nums leading-none ${t.tx}`}>
                          {w.hitRate != null ? `${w.hitRate}%` : "—"}
                        </div>
                        <div className={`text-[11px] mt-1 ${t.tx} opacity-80`}>
                          {w.inBand} of {w.scored} in band
                        </div>
                        <div className={`text-[11px] mt-1.5 tabular-nums ${t.tx} opacity-70`}>
                          said {Math.round(w.predicted)} · sold {Math.round(w.actual ?? 0)} units
                        </div>
                      </>
                    ) : (
                      <>
                        <div className={`mt-2.5 text-lg font-bold leading-tight ${t.tx}`}>
                          {w.due ? "Ready to grade" : "In progress"}
                        </div>
                        <div className={`text-[11px] mt-1 ${t.tx} opacity-80`}>
                          {w.products} product{w.products === 1 ? "" : "s"} · {Math.round(w.predicted)} units expected
                        </div>
                        <div className={`text-[11px] mt-1.5 ${t.tx} opacity-70`}>
                          grades {fmtDay(w.gradableFrom)}
                        </div>
                      </>
                    )}
                  </button>

                  {isOpen && (
                    <div className={`border-t ${lm ? "border-black/5" : "border-white/10"} ${lm ? "bg-white/60" : "bg-black/20"}`}>
                      <div className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-4 px-4 py-1.5 text-[10px] uppercase tracking-wider font-bold ${t.tx} opacity-70`}>
                        <span>Product</span><span className="text-right">Expected</span>
                        <span className="text-right">Sold</span><span className="text-right">Result</span>
                      </div>
                      {entries.map((e) => (
                        <div key={e.id}
                          onClick={() => onPickSku && onPickSku(e.sku)}
                          className={`grid grid-cols-[1fr_auto_auto_auto] gap-x-4 items-baseline px-4 py-1.5 text-[12px] border-t ${lm ? "border-black/5 hover:bg-white" : "border-white/5 hover:bg-white/5"} ${onPickSku ? "cursor-pointer" : ""}`}>
                          <span className={`truncate ${text}`} title={e.skuName}>
                            {e.skuName}
                            {e.amended && <span className={`ml-1.5 text-[9px] ${faint}`}>revised from {e.originalPredicted}</span>}
                          </span>
                          <span className={`text-right font-mono tabular-nums ${muted}`}>
                            {Math.round(e.predicted)}
                            {e.lo != null && <span className={faint}> ({Math.round(e.lo)}–{Math.round(e.hi)})</span>}
                          </span>
                          <span className={`text-right font-mono tabular-nums ${e.scoredAt ? text : faint}`}>
                            {e.scoredAt ? Math.round(e.actual) : "—"}
                          </span>
                          <span className={`text-right text-[11px] font-semibold ${
                            !e.scoredAt ? faint
                              : e.inBand === false ? (lm ? "text-rose-600" : "text-rose-400")
                              : (lm ? "text-emerald-600" : "text-emerald-400")}`}>
                            {!e.scoredAt ? (e.isDue ? "ready" : `${e.dueInDays}d`)
                              : e.inBand === false ? "outside" : "in band"}
                          </span>
                        </div>
                      ))}
                      <div className={`px-4 py-2 text-[10px] ${t.tx} opacity-70 border-t ${lm ? "border-black/5" : "border-white/5"}`}>
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

      <p className={`text-[11px] ${faint}`}>
        Weeks run Sunday to Saturday and are filed under the month holding most of their days. Forecasts are
        sealed when made; if a promotion is declared mid-week the tile is marked <span className="font-semibold">revised</span> and
        keeps both figures, so a campaign is never mistaken for a modelling win.
      </p>
    </div>
  );
}
