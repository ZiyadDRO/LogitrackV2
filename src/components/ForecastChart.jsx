import React from 'react';
import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ReferenceArea, ResponsiveContainer, Brush } from 'recharts';
import { todayMs } from '../lib/helpers';

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

  if (value === null) return null;

  const style = {
    padding: "6px 10px",
    background: lm ? "#ffffff" : "#161619",
    border: lm ? "1px solid #cbd5e1" : "1px solid #1e293b",
    borderRadius: 8,
    fontFamily: "ui-monospace, monospace",
    fontSize: 11,
    color: lm ? "#334155" : "#cbd5e1",
    boxShadow: lm ? "0 10px 15px -3px rgba(15,23,42,0.18)" : "0 10px 15px -3px rgba(0,0,0,0.5)",
  };

  return (
    <div style={style}>
      <div style={{ color: lm ? "#64748b" : "#475569", fontSize: 9, marginBottom: 2 }}>{dateStr}</div>
      <div><span style={{ fontWeight: "bold", color: lm ? "#0f172a" : "#fff" }}>{value.toLocaleString()}</span> units</div>
      {low != null && high != null && (
        <div style={{ color: lm ? "#64748b" : "#94a3b8", fontSize: 9, marginTop: 2 }}>{low.toLocaleString()} - {high.toLocaleString()} units range</div>
      )}
    </div>
  );
}

export function RefLineLabel({ viewBox, value, color, yOffset = 4 }) {
  return (
    <text x={viewBox.x + 4} y={viewBox.y + yOffset} fill={color} fontSize={9} fontFamily="ui-monospace, monospace" dominantBaseline="hanging">
      {value}
    </text>
  );
}

export function ForecastChart({ historyPoints, futurePoints, rangePoints, stockoutTime, reorderTime, intervalWidth, upcomingPromos, lm }) {
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
  const data = Array.from(dataMap.values()).sort((a, b) => a.x - b.x);

  const fmtDate = (ms) => new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  const ciLabel = intervalWidth ? `${intervalWidth}% Range` : "Range";
  const grid = lm ? "#e2e8f0" : "#1e293b";
  const tick = lm ? "#64748b" : "#475569";
  const bandMask = lm ? "#ffffff" : "#161619";
  const legendColor = lm ? "#475569" : "#94a3b8";
  const todayColor = lm ? "#64748b" : "#cbd5e1";

  const refLines = [
    { x: todayMs(),   stroke: lm ? "#94a3b8" : "#94a3b8", dash: "3 3", width: 1,   label: "Today",    color: todayColor, yOffset: 4  },
    reorderTime  && { x: reorderTime,  stroke: "#f59e0b", dash: "5 4", width: 1.5, label: "Order By", color: "#f59e0b", yOffset: 20 },
    stockoutTime && { x: stockoutTime, stroke: "#ef4444", dash: "5 4", width: 1.5, label: "Stockout", color: "#ef4444", yOffset: 36 },
  ].filter(Boolean);

  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 24, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={grid} vertical={false} />
        <XAxis dataKey="x" type="number" scale="time" domain={["dataMin", "dataMax"]} tickCount={8} tickFormatter={fmtDate}
          tick={{ fill: tick, fontSize: 9, fontFamily: "ui-monospace, monospace" }} axisLine={{ stroke: grid }} tickLine={{ stroke: grid }} />
        <YAxis domain={[0, "auto"]} tickFormatter={v => Math.round(v)} tick={{ fill: tick, fontSize: 9, fontFamily: "ui-monospace, monospace" }} axisLine={false} tickLine={false} width={36} />
        <Tooltip content={<ChartTooltip lm={lm} />} />
        <Legend wrapperStyle={{ fontSize: 10, fontFamily: "ui-monospace, monospace", color: legendColor, paddingBottom: 4 }} />

        {(upcomingPromos || []).map((p, i) => (
          <ReferenceArea key={i} x1={new Date(p.date + "T00:00:00Z").getTime()} x2={new Date((p.end_date || p.date) + "T00:00:00Z").getTime()}
            fill="#8b5cf6" fillOpacity={lm ? 0.12 : 0.08} label={{ value: p.label || "Promo", fill: "#8b5cf6", fontSize: 8 }} />
        ))}

        <Area name={ciLabel} dataKey="rangeHigh" stroke="none" fill="#ef4444" fillOpacity={lm ? 0.10 : 0.12} legendType="square" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="_rangeLow" dataKey="rangeLow" stroke="none" fill={bandMask} fillOpacity={1} legendType="none" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="Historical Sales" dataKey="y" stroke="#8b7dff" strokeWidth={1.5} fill="#8b7dff" fillOpacity={lm ? 0.10 : 0.07} isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: "#8b7dff" }} />
        <Line name="Forecast" dataKey="forecast" stroke="#ef4444" strokeWidth={2} strokeDasharray="5 4" isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: "#ef4444" }} />

        {refLines.map((rl, i) => (
          <ReferenceLine key={i} x={rl.x} stroke={rl.stroke} strokeWidth={rl.width} strokeDasharray={rl.dash}
            label={<RefLineLabel value={rl.label} color={rl.color} yOffset={rl.yOffset} />} />
        ))}

        <Brush dataKey="x" tickFormatter={fmtDate} height={20} stroke={lm ? "#cbd5e1" : "#1e293b"} fill={lm ? "#f8fafc" : "#161619"} travellerWidth={6}
          style={{ fontSize: 8, fontFamily: "ui-monospace, monospace" }} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export default ForecastChart;
