import React, { useState, useEffect, useRef } from 'react';
import { onApiError } from '../lib/api';

// ─── TOOLTIP HELPER ──────────────────────────
export function Tip({ text, children }) {
  const [show, setShow] = useState(false);
  const [coords, setCoords] = useState({ x: 0, y: 0 });
  const ref = useRef(null);
  const handleEnter = () => {
    if (ref.current) {
      const r = ref.current.getBoundingClientRect();
      setCoords({ x: r.left + r.width / 2, y: r.top });
    }
    setShow(true);
  };
  return (
    <span ref={ref} className="inline-flex items-center gap-0.5 cursor-default"
      onMouseEnter={handleEnter} onMouseLeave={() => setShow(false)}>
      {children}
      {show && (
        <span style={{ position: "fixed", left: coords.x, top: coords.y - 8, transform: "translate(-50%, -100%)", zIndex: 9999 }}
          className="px-2.5 py-1.5 text-[11px] leading-relaxed bg-slate-900 text-slate-100 border border-slate-700 rounded-lg pointer-events-none shadow-xl w-max max-w-[220px] text-center font-normal whitespace-normal tracking-normal">
          {text}
          <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-slate-700" />
        </span>
      )}
    </span>
  );
}

// ─── RELIABILITY BADGE ───────────────────────
export function ReliabilityBadge({ color, message, intervalWidth, demandVolatilityColor, lm }) {
  const dataStyles = {
    GREEN:  { pill: lm ? "bg-emerald-100 text-emerald-800 border-emerald-300" : "bg-emerald-950 text-emerald-400 border-emerald-800", label: "Strong data"    },
    YELLOW: { pill: lm ? "bg-amber-100 text-amber-800 border-amber-300"       : "bg-amber-950 text-amber-400 border-amber-800",       label: "Moderate data"  },
    ORANGE: { pill: lm ? "bg-orange-100 text-orange-800 border-orange-300"    : "bg-orange-950 text-orange-400 border-orange-800",    label: "Limited data"   },
    RED:    { pill: lm ? "bg-rose-100 text-rose-800 border-rose-300"          : "bg-rose-950 text-rose-400 border-rose-800",          label: "Thin data"      },
  };
  const volStyles = {
    GREEN:  { pill: lm ? "bg-emerald-100 text-emerald-800 border-emerald-300" : "bg-emerald-950 text-emerald-400 border-emerald-800", label: "Low noise"      },
    YELLOW: { pill: lm ? "bg-amber-100 text-amber-800 border-amber-300"       : "bg-amber-950 text-amber-400 border-amber-800",       label: "Moderate noise" },
    ORANGE: { pill: lm ? "bg-orange-100 text-orange-800 border-orange-300"    : "bg-orange-950 text-orange-400 border-orange-800",    label: "High noise"     },
  };
  const ds = dataStyles[color] || dataStyles.GREEN;
  const vs = demandVolatilityColor ? (volStyles[demandVolatilityColor] || volStyles.GREEN) : null;

  return (
    <div className="flex flex-col gap-2">
      <div className={`rounded-xl border overflow-hidden ${lm ? "border-slate-200" : "border-slate-700/50"}`}>
        {/* Data sufficiency row */}
        <div className={`flex items-center gap-2.5 px-3 py-2 ${lm ? "bg-white" : "bg-transparent"}`}>
          <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-widest border shrink-0 ${ds.pill}`}>
            {ds.label}
          </span>
          <div className={`text-[11px] leading-snug ${lm ? "text-slate-500" : "text-slate-500"}`}>
            <span className={`font-semibold ${lm ? "text-slate-600" : "text-slate-400"}`}>Forecast data quality — </span>
            how much history you have vs. how far ahead you're forecasting
          </div>
        </div>

        {/* Demand noise row */}
        {vs && (
          <div className={`flex items-center gap-2.5 px-3 py-2 border-t ${lm ? "border-slate-100 bg-slate-50/50" : "border-slate-700/30 bg-slate-800/20"}`}>
            <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-widest border shrink-0 ${vs.pill}`}>
              {vs.label}
            </span>
            <div className={`text-[11px] leading-snug ${lm ? "text-slate-500" : "text-slate-500"}`}>
              <span className={`font-semibold ${lm ? "text-slate-600" : "text-slate-400"}`}>Demand noise — </span>
              how random sales are after removing seasonality & trend
              {intervalWidth && (
                <span className={`ml-1 font-mono font-semibold ${lm ? "text-slate-500" : "text-slate-400"}`}>→ {intervalWidth}% forecast band</span>
              )}
            </div>
          </div>
        )}
      </div>
      {message && <p className={`text-xs ${lm ? "text-slate-500" : "text-slate-500"} leading-relaxed`}>{message}</p>}
    </div>
  );
}

// ─── MONTH FORECAST CARD ─────────────────────
export function MonthForecastCard({ card, lm }) {
  const borderColor = lm
    ? { GREEN: "border-slate-300", YELLOW: "border-amber-300", ORANGE: "border-orange-300", RED: "border-rose-300" }
    : { GREEN: "border-slate-700/60", YELLOW: "border-amber-800/50", ORANGE: "border-orange-800/50", RED: "border-rose-800/50" };
  const accentBar = { GREEN: "bg-emerald-500", YELLOW: "bg-amber-500", ORANGE: "bg-orange-500", RED: "bg-rose-500" };
  const dotColor  = { GREEN: "bg-emerald-500", YELLOW: "bg-amber-500", ORANGE: "bg-orange-500", RED: "bg-rose-500" };
  const bg = lm ? "bg-white" : "bg-[#161619]";
  const textMain = lm ? "text-slate-900" : "text-white";
  const textMuted = lm ? "text-slate-500" : "text-slate-500";
  const textSub = lm ? "text-slate-600" : "text-slate-300";
  const divider = lm ? "border-slate-200" : "border-white/10";
  return (
    <div className={`relative ${bg} border ${borderColor[card.reliabilityColor] || (lm ? "border-slate-300" : "border-slate-700")} rounded-2xl p-5 flex flex-col gap-4 overflow-hidden`}>
      <div className={`absolute top-0 left-0 right-0 h-0.5 ${accentBar[card.reliabilityColor]}`} />
      <div className="flex items-center justify-between">
        <span className={`text-sm font-bold ${textMain} tracking-tight`}>{card.monthLabel}</span>
        <div className="flex items-center gap-2">
          {card.intervalWidth && <span className={`text-[10px] font-mono ${textMuted}`}>{card.intervalWidth}% CI</span>}
          <div className={`h-1.5 w-1.5 rounded-full ${dotColor[card.reliabilityColor]}`} />
          <span className={`text-[11px] font-mono ${textMuted} uppercase tracking-widest`}>{card.isCurrent ? "Current" : "Forecast"}</span>
        </div>
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <span className={`text-3xl font-bold ${textMain} tabular-nums`}>{card.projectedTotal.toLocaleString()}</span>
          <span className={`text-xs ${textMuted}`}>units expected</span>
        </div>
        <div className={`text-xs ${textMuted} mt-1.5`}>Range: <span className={textSub}>{card.rangeLow.toLocaleString()} – {card.rangeHigh.toLocaleString()}</span></div>
      </div>
      {card.isCurrent && card.actualsSoFar > 0 && (
        <div className={`grid grid-cols-2 gap-3 text-xs pt-3 border-t ${divider}`}>
          <div className={textMuted}>Sold so far<span className={`block ${textMain} font-bold text-sm tabular-nums mt-0.5`}>{card.actualsSoFar.toLocaleString()}</span></div>
          <div className={textMuted}>Remaining<span className="block text-violet-500 font-bold text-sm tabular-nums mt-0.5">{card.forecastRemaining.toLocaleString()}</span></div>
        </div>
      )}
      {card.reliabilityMessage && (
        <div className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-600"} leading-relaxed border-t ${divider} pt-3`}>{card.reliabilityMessage}</div>
      )}
    </div>
  );
}

// ─── METRIC CARD ─────────────────────────────
export function MetricCard({ label, value, sub, valueColor, accent = false, lm }) {
  const bg = lm
    ? (accent ? "bg-violet-50 border-violet-300" : "bg-white border-slate-200")
    : (accent ? "bg-violet-950/30 border-violet-800/40" : "bg-[#161619] border-white/10");
  const textLabel = lm ? "text-slate-500" : "text-slate-500";
  const textSub   = lm ? "text-slate-500" : "text-slate-500";
  const valColor  = valueColor || (lm ? "text-slate-900" : "text-white");
  return (
    <div className={`${bg} border rounded-2xl p-4 flex flex-col gap-1`}>
      <span className={`text-[11px] ${textLabel} uppercase tracking-widest font-semibold flex items-center gap-1`}>{label}</span>
      <span className={`text-2xl font-bold tabular-nums ${valColor}`}>{value}</span>
      {sub && <span className={`text-[11px] ${textSub} leading-tight`}>{sub}</span>}
    </div>
  );
}

// ─── HISTORICAL SUMMARY TABLE ─────────────────
export function MonthlySummaryTable({ rows, lm }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? rows : rows.slice(-6);
  const max = Math.max(...rows.map(r => r.total), 1);
  const bg = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const textLabel = lm ? "text-slate-500" : "text-slate-500";
  const textVal = lm ? "text-slate-900" : "text-white";
  const barBg = lm ? "bg-slate-200" : "bg-slate-800";
  return (
    <div className={`${bg} border rounded-2xl p-5`}>
      <div className="flex items-center justify-between mb-4">
        <span className={`text-[11px] font-bold uppercase tracking-widest ${textLabel}`}>Historical Monthly Sales</span>
        {rows.length > 6 && (
          <button onClick={() => setExpanded(!expanded)} className="text-[11px] text-violet-500 hover:text-violet-600 transition-colors">
            {expanded ? "Show less" : `All ${rows.length} months`}
          </button>
        )}
      </div>
      <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
        {visible.map((row, i) => (
          <div key={i} className="flex items-center gap-3">
            <span className={`text-xs ${textLabel} w-24 shrink-0`}>{row.month}</span>
            <div className={`flex-1 h-1.5 ${barBg} rounded-full overflow-hidden`}>
              <div className="h-full bg-violet-500/60 rounded-full" style={{ width: `${(row.total / max) * 100}%` }} />
            </div>
            <span className={`text-xs ${textVal} font-mono font-bold w-16 text-right tabular-nums`}>{row.total.toLocaleString()}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── PRICE TIER TABLE ────────────────────────
export const TIER_STYLES = {
  reliable:     { dot: "bg-emerald-400", label: "Reliable",     tc: (lm) => lm ? "text-emerald-700" : "text-emerald-400" },
  moderate:     { dot: "bg-violet-400",     label: "Moderate",     tc: (lm) => lm ? "text-violet-700"     : "text-violet-400"     },
  weak:         { dot: "bg-amber-400",   label: "Weak",         tc: (lm) => lm ? "text-amber-700"   : "text-amber-400"   },
  insufficient: { dot: "bg-rose-400",    label: "Insufficient", tc: (lm) => lm ? "text-rose-700"    : "text-rose-400"    },
};

export function PriceTierTable({ tiers, lm }) {
  if (!tiers || tiers.length === 0) return null;
  return (
    <div className="mt-2 space-y-1.5">
      {tiers.map((tier, i) => {
        const ts = TIER_STYLES[tier.quality] || TIER_STYLES.insufficient;
        return (
          <div key={i} className="flex items-center gap-2 font-mono text-[11px]">
            <div className={`h-1.5 w-1.5 rounded-full shrink-0 ${ts.dot}`} />
            <span className={lm ? "text-slate-700" : "text-slate-300"}>${tier.price.toFixed(2)}</span>
            <span className={lm ? "text-slate-400" : "text-slate-500"}>{tier.days}d</span>
            <span className={ts.tc(lm)}>{ts.label}</span>
          </div>
        );
      })}
      <p className={`text-[10px] mt-1 ${lm ? "text-slate-400" : "text-slate-600"}`}>
        Reliable ≥90d · Moderate 60–89d · Weak 30–59d · Insufficient &lt;30d
      </p>
    </div>
  );
}

// Small status pill used by the SKU signal strip.
export function SignalPill({ tone, lm, children }) {
  const cls = {
    emerald: lm ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-emerald-950/40 text-emerald-300 border-emerald-900/50",
    amber:   lm ? "bg-amber-50 text-amber-700 border-amber-200"       : "bg-amber-950/30 text-amber-300 border-amber-900/50",
    orange:  lm ? "bg-orange-50 text-orange-700 border-orange-200"    : "bg-orange-950/30 text-orange-300 border-orange-900/50",
    rose:    lm ? "bg-rose-50 text-rose-700 border-rose-200"          : "bg-rose-950/30 text-rose-300 border-rose-900/50",
    slate:   lm ? "bg-slate-100 text-slate-600 border-slate-200"      : "bg-slate-800/50 text-slate-300 border-slate-700/50",
  }[tone] || (lm ? "bg-slate-100 text-slate-600 border-slate-200" : "bg-slate-800/50 text-slate-300 border-slate-700/50");
  return <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold border whitespace-nowrap ${cls}`}>{children}</span>;
}

// ─── ERROR TOASTS ─────────────────────────────
// Renders backend-call failures reported through lib/api's fetchJson. Before
// this, every failed request was silently swallowed and SKUs just vanished.
export function ErrorToasts({ lm }) {
  const [toasts, setToasts] = useState([]);
  useEffect(() => onApiError(t => {
    setToasts(prev => [...prev.slice(-3), t]);
    setTimeout(() => setToasts(prev => prev.filter(x => x.id !== t.id)), 7000);
  }), []);
  if (!toasts.length) return null;
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] flex flex-col gap-2 items-center pointer-events-none">
      {toasts.map(t => (
        <div key={t.id}
          className={`pointer-events-auto flex items-start gap-2.5 max-w-md rounded-xl border px-4 py-2.5 text-xs leading-relaxed shadow-2xl ${lm ? "bg-white border-rose-300 text-rose-700" : "bg-[#1c1114] border-rose-900/60 text-rose-300"}`}>
          <span className="shrink-0 mt-0.5">⚠️</span>
          <span className="flex-1">{t.message}</span>
          <button onClick={() => setToasts(prev => prev.filter(x => x.id !== t.id))}
            className={`shrink-0 ${lm ? "text-rose-400 hover:text-rose-600" : "text-rose-500 hover:text-rose-300"}`}>✕</button>
        </div>
      ))}
    </div>
  );
}
