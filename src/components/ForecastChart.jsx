import React from 'react';
import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ReferenceArea, ResponsiveContainer, Brush } from 'recharts';
import { todayMs } from '../lib/helpers';
import { terminal, MONO } from '../lib/theme';

// ─── CHART ───────────────────────────────────

export function ChartTooltip({ active, payload, label, lm }) {
  if (!active || !payload || !payload.length) return null;

  const dateStr = label
    ? new Date(label).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })
    : "";

  let value = null;
  let low = null;
  let high = null;

  for (const p of payload) {
    if (p.dataKey === "y" && p.value != null && !isNaN(p.value)) value = Math.round(p.value);
    if (p.dataKey === "forecast" && p.value != null && !isNaN(p.value)) value = Math.round(p.value);
    if (p.dataKey === "rangeLow" && p.value != null && !isNaN(p.value)) low = Math.round(p.value);
    if (p.dataKey === "rangeHigh" && p.value != null && !isNaN(p.value)) high = Math.round(p.value);
  }
  const todayP = payload.find(p => p.dataKey === "todaySoFar" && p.value != null);

  if (value === null && !todayP) return null;

  const T = terminal(lm);
  const style = {
    padding: "7px 11px",
    background: T.panel,
    border: `2px solid ${T.line2}`,
    borderRadius: 2,
    fontFamily: MONO,
    fontSize: 12.5,
    color: T.ink,
    boxShadow: lm ? "0 10px 15px -3px rgba(15,23,42,0.18)" : "0 10px 15px -3px rgba(0,0,0,0.5)",
  };

  return (
    <div style={style}>
      <div style={{ color: T.dim, fontSize: 11, marginBottom: 3 }}>{dateStr}</div>
      {todayP && (
        <div><span style={{ fontWeight: 600, color: T.ink }}>{Math.round(todayP.value).toLocaleString()}</span> sold so far today
          <div style={{ color: T.soft, fontSize: 11 }}>
            partial day{todayP.payload?.todayStale ? " · last check over an hour old" : ""} · not in the forecast yet
          </div>
        </div>
      )}
      {value !== null && (
        <div><span style={{ fontWeight: 600, color: T.ink }}>{value.toLocaleString()}</span> units{todayP ? " forecast" : ""}</div>
      )}
      {low != null && high != null && (
        <div style={{ color: T.soft, fontSize: 11.5, marginTop: 3 }}>{low.toLocaleString()} to {high.toLocaleString()} units range</div>
      )}
    </div>
  );
}

/* Reference-line labels sit inside the plot, so one near the right edge would be
   clipped by the axis. `flip` anchors it to the left of its own line instead. */
export function RefLineLabel({ viewBox, value, color, yOffset = 4, flip = false }) {
  return (
    <text x={viewBox.x + (flip ? -4 : 4)} y={viewBox.y + yOffset} fill={color} fontSize={9}
      fontFamily="ui-monospace, monospace" dominantBaseline="hanging" textAnchor={flip ? "end" : "start"}>
      {value}
    </text>
  );
}

export function ForecastChart({ historyPoints, futurePoints, rangePoints, stockoutTime, reorderTime, poLandsTime = null, intervalWidth, upcomingPromos, holidayWindows = [], lm, todaySales = null }) {
  /* The strip's labels are laid out in pixels, so it has to know how wide it
     actually is — percent-only layout is what let labels overrun each other and
     the card edge. Hooks run before any early return. */
  const roRef = React.useRef(null);
  const [stripW, setStripW] = React.useState(0);
  /* A callback ref rather than useRef + effect: the strip only mounts once there is
     more than one mark, which can happen on a later render, and a callback ref
     measures whenever that actually occurs. */
  const stripRef = React.useCallback((node) => {
    if (roRef.current) { roRef.current.disconnect(); roRef.current = null; }
    if (!node) return;
    setStripW(node.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setStripW(node.clientWidth));
    ro.observe(node);
    roRef.current = ro;
  }, []);
  React.useEffect(() => () => roRef.current?.disconnect(), []);

  if (!historyPoints || !futurePoints) return null;

  const dataMap = new Map();
  for (const p of historyPoints) dataMap.set(p.x, { x: p.x, y: p.y });
  for (let i = 0; i < futurePoints.length; i++) {
    const fp = futurePoints[i];
    const rp = rangePoints?.[i];
    const existing = dataMap.get(fp.x) || { x: fp.x };
    existing.forecast  = fp.y;
    existing.rangeLow  = rp?.y?.[0] ?? null;
    existing.rangeHigh = rp?.y?.[1] ?? null;
    dataMap.set(fp.x, existing);
  }
  /* TODAY SO FAR — its own series, never folded into `y`.
     The history line stops at the last COMPLETE day on purpose (a day in progress is not
     a small day, it is an unknown one), which left the chart silent about the hours
     since midnight: a shop that had traded all day looked exactly like one that had not.
     This marks today's running total as a separate, visibly different point so it can
     never be read as a finished day or pull the history line down to meet it. */
  if (todaySales && todaySales.forToday && !todaySales.unsupported && todaySales.day) {
    const tx = Date.parse(`${todaySales.day}T00:00:00Z`);
    if (Number.isFinite(tx)) {
      const existing = dataMap.get(tx) || { x: tx };
      existing.todaySoFar = Number(todaySales.units) || 0;
      existing.todayStale = !!todaySales.stale;
      dataMap.set(tx, existing);
    }
  }
  const data = Array.from(dataMap.values()).sort((a, b) => a.x - b.x);

  const fmtDate = (ms) => new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  const ciLabel = intervalWidth ? `${intervalWidth}% Range` : "Range";
  /* Recharts takes colours as props, not classes, so the chart reads the palette
     object directly. Historical sales was violet and the forecast was red — the same
     red the Stockout reference line uses, which made the forecast itself look like a
     warning. Actual is ink, forecast is the amber accent, the band is amber too. */
  const CT = terminal(lm);
  const grid = CT.line;
  const tick = CT.dim;
  const bandMask = CT.panel;
  const legendColor = CT.soft;
  const todayColor = CT.soft;

  /* Same domain recharts will use, so the strip's marks land on the chart's own
     x positions. `left` matches the YAxis width and `right` the chart margin —
     a strip a few pixels out of register is worse than no strip. */
  const AX_L = 36, AX_R = 16;
  const x0 = data.length ? data[0].x : 0;
  const x1 = data.length ? data[data.length - 1].x : 1;
  const span = Math.max(1, x1 - x0);
  const pctOf = t => ((t - x0) / span) * 100;

  const marks = [
    { t: todayMs(),  label: "TODAY",     color: CT.soft },
    reorderTime  && { t: reorderTime,  label: "ORDER BY", color: CT.blue },
    poLandsTime  && { t: poLandsTime,  label: "PO LANDS", color: CT.blue },
    stockoutTime && { t: stockoutTime, label: "RUNS OUT", color: CT.red },
  ].filter(Boolean).filter(m => m.t >= x0 && m.t <= x1);
  const leadGap = (reorderTime && stockoutTime && stockoutTime > reorderTime)
    ? { a: reorderTime, b: stockoutTime, days: Math.round((stockoutTime - reorderTime) / 864e5) } : null;

  /* Marks a fortnight apart sit ~2% apart on a two-year axis, so their labels used
     to print straight through each other. Lay them out in real pixels instead:
     measure each block, then slide blocks sideways until none overlap, keeping them
     in date order and inside the strip. Sideways first, because stacking four
     clustered marks into four rows would eat the plot area; a second row only opens
     when the row genuinely cannot hold them. A leader line ties each label back to
     its own tick so a nudged label still reads as belonging to that date. */
  const W = stripW || 640;
  const CHAR_CODE = 7.0;   // 10px mono + .1em tracking
  const CHAR_DATE = 6.9;   // 11.5px mono
  const PAD = 12, GUTTER = 10;
  const pxOf = t => ((t - x0) / span) * W;

  const sized = [...marks].sort((a, b) => a.t - b.t).map(m => {
    const dateStr = fmtDate(m.t);
    return { ...m, dateStr, w: Math.max(m.label.length * CHAR_CODE, dateStr.length * CHAR_DATE) + PAD, center: pxOf(m.t) };
  });

  /* Push right off each neighbour, then pull the run back inside the right edge,
     then re-settle against the left edge. Order is preserved throughout. */
  const spread = (items) => {
    const n = items.length;
    if (!n) return [];
    const pos = items.map(it => it.center - it.w / 2);
    for (let i = 1; i < n; i++) pos[i] = Math.max(pos[i], pos[i - 1] + items[i - 1].w + GUTTER);
    const over = pos[n - 1] + items[n - 1].w - W;
    if (over > 0) {
      pos[n - 1] -= over;
      for (let i = n - 2; i >= 0; i--) pos[i] = Math.min(pos[i], pos[i + 1] - items[i].w - GUTTER);
    }
    pos[0] = Math.max(pos[0], 0);
    for (let i = 1; i < n; i++) pos[i] = Math.max(pos[i], pos[i - 1] + items[i - 1].w + GUTTER);
    return pos;
  };

  /* One row is preferred, but a label dragged halfway across the chart from its own
     date is no clearer than an overlapping one. Spread sideways first; if that would
     push a label more than MAX_SHIFT from its tick, open another row (which halves
     the span each row has to cover) and keep whichever attempt drags labels least.
     Three rows is the ceiling — beyond that the strip costs more than it explains. */
  const MAX_SHIFT = 90;
  const attempt = (nRows) => {
    const buckets = Array.from({ length: nRows }, () => []);
    sized.forEach((m, i) => buckets[i % nRows].push(m));
    const out = buckets.flatMap((items, row) => {
      const pos = spread(items);
      return items.map((m, i) => ({ ...m, row, left: pos[i] }));
    });
    const shift = out.reduce((mx, m) => Math.max(mx, Math.abs(m.left + m.w / 2 - m.center)), 0);
    return { out, shift };
  };
  let best = attempt(1);
  for (let n = 2; n <= 3 && n <= sized.length && best.shift > MAX_SHIFT; n++) {
    const next = attempt(n);
    if (next.shift < best.shift) best = next;
  }
  const laidOut = [...best.out].sort((a, b) => a.t - b.t);

  const rows = laidOut.reduce((n, m) => Math.max(n, m.row + 1), 1);

  const ROW_H = 30, LABEL_H = 27, CAP_H = leadGap ? 18 : 4, TICK_H = 12;
  const CAP_TOP = rows * ROW_H;
  const TICK_TOP = CAP_TOP + CAP_H;
  const STRIP_H = TICK_TOP + TICK_H;

  /* The lead-time caption gets its own band under the labels rather than floating
     over them, and is clamped to the strip so the card can no longer cut it off.
     On a long axis the gap is only a few pixels wide, and a bracket that thin sits
     entirely behind its own caption — so it is drawn only when it can be seen. */
  const capText = leadGap ? `${leadGap.days} days of lead time: why that date is the deadline` : "";
  const capShort = leadGap ? `${leadGap.days}d lead time` : "";
  const capA = leadGap ? pxOf(leadGap.a) : 0;
  const capB = leadGap ? pxOf(leadGap.b) : 0;
  const capFull = capText.length * 5.9;
  const useFull = capFull + 24 < W;
  const capLabel = useFull ? capText : capShort;
  const capW = (useFull ? capFull : capShort.length * 5.9) + 14;
  const capLeft = Math.min(Math.max((capA + capB) / 2 - capW / 2, 0), Math.max(0, W - capW));
  const showBracket = capB - capA > capW + 20;

  const refLines = [
    { x: todayMs(),   stroke: CT.soft,  dash: "3 3", width: 1,   label: "Today",    color: todayColor, yOffset: 4  },
    reorderTime  && { x: reorderTime,  stroke: CT.blue,   dash: "5 4", width: 1.5, label: "Order By", color: CT.blue, yOffset: 20 },
    stockoutTime && { x: stockoutTime, stroke: CT.red,    dash: "5 4", width: 1.5, label: "Stockout", color: CT.red, yOffset: 36 },
  ].filter(Boolean).map(rl => ({ ...rl, flip: pctOf(rl.x) > 86 }));

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {marks.length > 1 && (
        <div ref={stripRef} style={{ position: "relative", height: STRIP_H, marginLeft: AX_L, marginRight: AX_R,
          flexShrink: 0, fontFamily: MONO }}>
          {/* Leaders and ticks share one SVG layer so they stay in exact register
              with the pixel-positioned labels above them. */}
          <svg width="100%" height={STRIP_H} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
            {laidOut.map((m, i) => {
              const lx = m.left + m.w / 2, ly = m.row * ROW_H + LABEL_H;
              return (
                <g key={i}>
                  <polyline
                    points={`${lx},${ly} ${lx},${ly + 5} ${m.center},${TICK_TOP - 5} ${m.center},${TICK_TOP}`}
                    fill="none" stroke={m.color} strokeWidth={1} opacity={0.45} />
                  <line x1={m.center} y1={TICK_TOP} x2={m.center} y2={STRIP_H}
                    stroke={m.color} strokeWidth={1} opacity={0.6} />
                </g>
              );
            })}
            {leadGap && showBracket && (
              <g stroke={CT.amber} strokeWidth={1.5} opacity={0.55} strokeDasharray="4 3">
                <line x1={capA} y1={CAP_TOP + 8} x2={capB} y2={CAP_TOP + 8} />
                <line x1={capA} y1={CAP_TOP + 3} x2={capA} y2={CAP_TOP + 13} strokeDasharray="0" />
                <line x1={capB} y1={CAP_TOP + 3} x2={capB} y2={CAP_TOP + 13} strokeDasharray="0" />
              </g>
            )}
          </svg>
          {leadGap && (
            <div style={{ position: "absolute", left: capLeft, top: CAP_TOP, whiteSpace: "nowrap",
              fontSize: 11, lineHeight: "16px", color: CT.amber, background: CT.panel, padding: "0 7px" }}>
              {capLabel}
            </div>
          )}
          {laidOut.map((m, i) => (
            <div key={i} style={{ position: "absolute", left: m.left, width: m.w, top: m.row * ROW_H,
              padding: "0 6px", boxSizing: "border-box", whiteSpace: "nowrap", textAlign: "center" }}>
              <div style={{ fontSize: 10, letterSpacing: ".1em", fontWeight: 600, lineHeight: "13px", color: m.color }}>{m.label}</div>
              <div style={{ fontSize: 11.5, lineHeight: "14px", color: CT.soft }}>{m.dateStr}</div>
            </div>
          ))}
        </div>
      )}
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 24, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={grid} vertical={false} />
        <XAxis dataKey="x" type="number" scale="time" domain={["dataMin", "dataMax"]} tickCount={8} tickFormatter={fmtDate}
          tick={{ fill: tick, fontSize: 11, fontFamily: MONO }} axisLine={{ stroke: grid }} tickLine={{ stroke: grid }} />
        <YAxis domain={[0, "auto"]} tickFormatter={v => Math.round(v)} tick={{ fill: tick, fontSize: 11, fontFamily: MONO }} axisLine={false} tickLine={false} width={36} />
        <Tooltip content={<ChartTooltip lm={lm} />} />
        <Legend wrapperStyle={{ fontSize: 10, fontFamily: "ui-monospace, monospace", color: legendColor, paddingBottom: 4 }} />

        {/* Holidays the forecast lifts (or lowers), shaded on their own dates. Grey, so
            they don't compete with the promotions you entered, and unlabelled because
            neighbouring holidays would print over each other: the line under the chart
            names them. Day spans run to the end of the last day. */}
        {(holidayWindows || []).map((h, i) => (
          <ReferenceArea key={`h${i}`} x1={new Date(h.start + "T00:00:00Z").getTime()}
            x2={new Date(h.end + "T00:00:00Z").getTime() + 86400000}
            fill={CT.ink} fillOpacity={lm ? 0.05 : 0.06} />
        ))}
        {(upcomingPromos || []).map((p, i) => (
          <ReferenceArea key={i} x1={new Date(p.date + "T00:00:00Z").getTime()} x2={new Date((p.end_date || p.date) + "T00:00:00Z").getTime()}
            fill={CT.blue} fillOpacity={lm ? 0.12 : 0.10} label={{ value: p.label || "Promo", fill: CT.blue, fontSize: 11 }} />
        ))}

        <Area name={ciLabel} dataKey="rangeHigh" stroke="none" fill={CT.amber} fillOpacity={lm ? 0.14 : 0.16} legendType="square" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="_rangeLow" dataKey="rangeLow" stroke="none" fill={bandMask} fillOpacity={1} legendType="none" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="Historical Sales" dataKey="y" stroke={CT.ink} strokeWidth={1.6} fill={CT.ink} fillOpacity={lm ? 0.07 : 0.05} isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: CT.ink }} />
        <Line name="Forecast" dataKey="forecast" stroke={CT.amber} strokeWidth={2} strokeDasharray="5 4" isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: CT.amber }} />
        {/* A ring, not a filled dot, and no connecting line: it is a partial reading. */}
        <Line name="Today so far" dataKey="todaySoFar" stroke="none" isAnimationActive={false}
          legendType="circle" connectNulls={false}
          dot={{ r: 5, stroke: CT.ink, strokeWidth: 2, fill: CT.panel }}
          activeDot={{ r: 6, stroke: CT.ink, strokeWidth: 2, fill: CT.panel }} />

        {refLines.map((rl, i) => (
          <ReferenceLine key={i} x={rl.x} stroke={rl.stroke} strokeWidth={rl.width} strokeDasharray={rl.dash}
            label={<RefLineLabel value={rl.label} color={rl.color} yOffset={rl.yOffset} flip={rl.flip} />} />
        ))}

        <Brush dataKey="x" tickFormatter={fmtDate} height={20} stroke={CT.line2} fill={CT.sunken} travellerWidth={6}
          style={{ fontSize: 8, fontFamily: "ui-monospace, monospace" }} />
      </ComposedChart>
    </ResponsiveContainer>
    </div>
  );
}

export default ForecastChart;
