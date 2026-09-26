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
          className="px-2.5 py-1.5 text-[14px] leading-relaxed bg-[var(--t-bg)] text-[var(--t-ink)] border border-[var(--t-line2)] rounded-lg pointer-events-none shadow-xl w-max max-w-[220px] text-center font-normal whitespace-normal tracking-normal">
          {text}
          <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-[var(--t-line2)]" />
        </span>
      )}
    </span>
  );
}

// ─── RELIABILITY BADGE ───────────────────────
export function ReliabilityBadge({ color, message, intervalWidth, demandVolatilityColor, lm }) {
  const dataStyles = {
    GREEN:  { pill: "bg-[var(--t-good-soft)] text-[var(--t-good)] border-[var(--t-good-line)]", label: "Strong data"    },
    YELLOW: { pill: "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",       label: "Moderate data"  },
    ORANGE: { pill: "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",    label: "Limited data"   },
    RED:    { pill: "bg-[var(--t-bad-soft)] text-[var(--t-bad)] border-[var(--t-bad-line)]",          label: "Thin data"      },
  };
  const volStyles = {
    GREEN:  { pill: "bg-[var(--t-good-soft)] text-[var(--t-good)] border-[var(--t-good-line)]", label: "Low noise"      },
    YELLOW: { pill: "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",       label: "Moderate noise" },
    ORANGE: { pill: "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",    label: "High noise"     },
  };
  const ds = dataStyles[color] || dataStyles.GREEN;
  const vs = demandVolatilityColor ? (volStyles[demandVolatilityColor] || volStyles.GREEN) : null;

  return (
    <div className="flex flex-col gap-2">
      <div className={`rounded-xl border overflow-hidden ${"border-[var(--t-line)]"}`}>
        {/* Data sufficiency row */}
        <div className={`flex items-center gap-2.5 px-3 py-2 ${"bg-[var(--t-panel)]"}`}>
          <span className={`px-2 py-0.5 rounded text-[13px] font-bold uppercase tracking-widest border shrink-0 ${ds.pill}`}>
            {ds.label}
          </span>
          <div className={`text-[14px] leading-snug ${"text-[var(--t-dim)]"}`}>
            <span className={`font-semibold ${"text-[var(--t-soft)]"}`}>Forecast data quality: </span>
            how much history you have vs. how far ahead you're forecasting
          </div>
        </div>

        {/* Demand noise row */}
        {vs && (
          <div className={`flex items-center gap-2.5 px-3 py-2 border-t ${"border-[var(--t-line)] bg-[var(--t-sunken)]"}`}>
            <span className={`px-2 py-0.5 rounded text-[13px] font-bold uppercase tracking-widest border shrink-0 ${vs.pill}`}>
              {vs.label}
            </span>
            <div className={`text-[14px] leading-snug ${"text-[var(--t-dim)]"}`}>
              <span className={`font-semibold ${"text-[var(--t-soft)]"}`}>Demand noise: </span>
              how random sales are after removing seasonality & trend
              {intervalWidth && (
                <span className={`ml-1 font-mono font-semibold ${"text-[var(--t-dim)]"}`}>→ {intervalWidth}% forecast band</span>
              )}
            </div>
          </div>
        )}
      </div>
      {message && <p className={`text-[15px] ${"text-[var(--t-dim)]"} leading-relaxed`}>{message}</p>}
    </div>
  );
}

// ─── MONTH FORECAST CARD ─────────────────────
export function MonthForecastCard({ card, lm }) {
  const borderColor = lm
    ? { GREEN: "border-[var(--t-line2)]", YELLOW: "border-[var(--t-warn-line)]", ORANGE: "border-[var(--t-warn-line)]", RED: "border-[var(--t-bad-line)]" }
    : { GREEN: "border-[var(--t-line2)]", YELLOW: "border-[var(--t-warn-line)]", ORANGE: "border-[var(--t-warn-line)]", RED: "border-[var(--t-bad-line)]" };
  const accentBar = { GREEN: "bg-[var(--t-good-soft)]", YELLOW: "bg-[var(--t-warn-soft)]", ORANGE: "bg-[var(--t-warn-soft)]", RED: "bg-[var(--t-bad-soft)]" };
  const dotColor  = { GREEN: "bg-[var(--t-good-soft)]", YELLOW: "bg-[var(--t-warn-soft)]", ORANGE: "bg-[var(--t-warn-soft)]", RED: "bg-[var(--t-bad-soft)]" };
  const bg = "bg-[var(--t-panel)]";
  const textMain = "text-[var(--t-ink)]";
  const textMuted = "text-[var(--t-dim)]";
  const textSub = "text-[var(--t-soft)]";
  const divider = "border-[var(--t-line)]";
  return (
    <div className={`relative ${bg} border ${borderColor[card.reliabilityColor] || ("border-[var(--t-line2)]")} rounded-2xl p-5 flex flex-col gap-4 overflow-hidden`}>
      <div className={`absolute top-0 left-0 right-0 h-0.5 ${accentBar[card.reliabilityColor]}`} />
      <div className="flex items-center justify-between">
        <span className={`text-[16.5px] font-bold ${textMain} tracking-tight`}>{card.monthLabel}</span>
        <div className="flex items-center gap-2">
          {card.intervalWidth && <span className={`text-[13px] font-mono ${textMuted}`}>{card.intervalWidth}% CI</span>}
          <div className={`h-1.5 w-1.5 rounded-full ${dotColor[card.reliabilityColor]}`} />
          <span className={`text-[14px] font-mono ${textMuted} uppercase tracking-widest`}>{card.isCurrent ? "Current" : "Forecast"}</span>
        </div>
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <span className={`text-3xl font-bold ${textMain} tabular-nums`}>{card.projectedTotal.toLocaleString()}</span>
          <span className={`text-[15px] ${textMuted}`}>units expected</span>
        </div>
        <div className={`text-[15px] ${textMuted} mt-1.5`}>Range: <span className={textSub}>{card.rangeLow.toLocaleString()} to {card.rangeHigh.toLocaleString()}</span></div>
      </div>
      {card.isCurrent && card.actualsSoFar > 0 && (
        <div className={`grid grid-cols-2 gap-3 text-[15px] pt-3 border-t ${divider}`}>
          <div className={textMuted}>Sold so far<span className={`block ${textMain} font-bold text-[16.5px] tabular-nums mt-0.5`}>{card.actualsSoFar.toLocaleString()}</span></div>
          <div className={textMuted}>Remaining<span className="block text-[var(--t-accent)] font-bold text-[16.5px] tabular-nums mt-0.5">{card.forecastRemaining.toLocaleString()}</span></div>
        </div>
      )}
      {card.reliabilityMessage && (
        <div className={`text-[14px] ${"text-[var(--t-dim)]"} leading-relaxed border-t ${divider} pt-3`}>{card.reliabilityMessage}</div>
      )}
    </div>
  );
}

// ─── METRIC CARD ─────────────────────────────
export function MetricCard({ label, value, sub, valueColor, accent = false, lm }) {
  const bg = lm
    ? (accent ? "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]" : "bg-[var(--t-panel)] border-[var(--t-line)]")
    : (accent ? "bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]" : "bg-[#161619] border-[var(--t-line)]");
  const textLabel = "text-[var(--t-dim)]";
  const textSub   = "text-[var(--t-dim)]";
  const valColor  = valueColor || ("text-[var(--t-ink)]");
  return (
    <div className={`${bg} border rounded-2xl p-4 flex flex-col gap-1`}>
      <span className={`text-[14px] ${textLabel} uppercase tracking-widest font-semibold flex items-center gap-1`}>{label}</span>
      <span className={`text-2xl font-bold tabular-nums ${valColor}`}>{value}</span>
      {sub && <span className={`text-[14px] ${textSub} leading-tight`}>{sub}</span>}
    </div>
  );
}

// ─── HISTORICAL SUMMARY TABLE ─────────────────
export function MonthlySummaryTable({ rows, lm }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? rows : rows.slice(-6);
  const max = Math.max(...rows.map(r => r.total), 1);
  const bg = "bg-[var(--t-panel)] border-[var(--t-line)]";
  const textLabel = "text-[var(--t-dim)]";
  const textVal = "text-[var(--t-ink)]";
  const barBg = "bg-[var(--t-sunken)]";
  return (
    <div className={`${bg} border rounded-2xl p-5`}>
      <div className="flex items-center justify-between mb-4">
        <span className={`text-[14px] font-bold uppercase tracking-widest ${textLabel}`}>Historical Monthly Sales</span>
        {rows.length > 6 && (
          <button onClick={() => setExpanded(!expanded)} className="text-[14px] text-[var(--t-accent)] hover:text-[var(--t-accent)] transition-colors">
            {expanded ? "Show less" : `All ${rows.length} months`}
          </button>
        )}
      </div>
      <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
        {visible.map((row, i) => (
          <div key={i} className="flex items-center gap-3">
            <span className={`text-[15px] ${textLabel} w-24 shrink-0`}>{row.month}</span>
            <div className={`flex-1 h-1.5 ${barBg} rounded-full overflow-hidden`}>
              <div className="h-full bg-[var(--t-accent-soft)] rounded-full" style={{ width: `${(row.total / max) * 100}%` }} />
            </div>
            <span className={`text-[15px] ${textVal} font-mono font-bold w-16 text-right tabular-nums`}>{row.total.toLocaleString()}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── PRICE TIER TABLE ────────────────────────
export const TIER_STYLES = {
  reliable:     { dot: "bg-[var(--t-good-soft)]", label: "Reliable",     tc: (lm) => "text-[var(--t-good)]" },
  moderate:     { dot: "bg-[var(--t-accent-soft)]",     label: "Moderate",     tc: (lm) => "text-[var(--t-accent)]"     },
  weak:         { dot: "bg-[var(--t-warn-soft)]",   label: "Weak",         tc: (lm) => "text-[var(--t-warn)]"   },
  insufficient: { dot: "bg-[var(--t-bad-soft)]",    label: "Insufficient", tc: (lm) => "text-[var(--t-bad)]"    },
};

export function PriceTierTable({ tiers, lm }) {
  if (!tiers || tiers.length === 0) return null;
  return (
    <div className="mt-2 space-y-1.5">
      {tiers.map((tier, i) => {
        const ts = TIER_STYLES[tier.quality] || TIER_STYLES.insufficient;
        return (
          <div key={i} className="flex items-center gap-2 font-mono text-[14px]">
            <div className={`h-1.5 w-1.5 rounded-full shrink-0 ${ts.dot}`} />
            <span className={"text-[var(--t-soft)]"}>${tier.price.toFixed(2)}</span>
            <span className={"text-[var(--t-dim)]"}>{tier.days}d</span>
            <span className={ts.tc(lm)}>{ts.label}</span>
          </div>
        );
      })}
      <p className={`text-[13px] mt-1 ${"text-[var(--t-dim)]"}`}>
        Reliable ≥90d · Moderate 60-89d · Weak 30-59d · Insufficient &lt;30d
      </p>
    </div>
  );
}

// Small status pill used by the SKU signal strip.
export function SignalPill({ tone, lm, children }) {
  const cls = {
    emerald: "bg-[var(--t-good-soft)] text-[var(--t-good)] border-[var(--t-good-line)]",
    amber:   "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",
    orange:  "bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]",
    rose:    "bg-[var(--t-bad-soft)] text-[var(--t-bad)] border-[var(--t-bad-line)]",
    slate:   "bg-[var(--t-sunken)] text-[var(--t-soft)] border-[var(--t-line)]",
  }[tone] || ("bg-[var(--t-sunken)] text-[var(--t-soft)] border-[var(--t-line)]");
  return <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[14px] font-semibold border whitespace-nowrap ${cls}`}>{children}</span>;
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
          className={`pointer-events-auto flex items-start gap-2.5 max-w-md rounded-xl border px-4 py-2.5 text-[15px] leading-relaxed shadow-2xl ${"bg-[var(--t-panel)] border-[var(--t-bad-line)] text-[var(--t-bad)]"}`}>
          <span className="shrink-0 mt-0.5">⚠️</span>
          <span className="flex-1">{t.message}</span>
          <button onClick={() => setToasts(prev => prev.filter(x => x.id !== t.id))}
            className={`shrink-0 ${"text-[var(--t-bad)] hover:text-[var(--t-bad)]"}`}>✕</button>
        </div>
      ))}
    </div>
  );
}
