import React from 'react';
import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ReferenceArea, ReferenceDot, ResponsiveContainer, Brush } from 'recharts';
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

const CLOSED_ORANGE = "#F07A1A";

function wholeNumberAxis(data) {
  const max = (data || []).reduce((highest, point) => {
    return Math.max(highest, ...[point.y, point.forecast, point.rangeHigh, point.todaySoFar]
      .map(value => Number.isFinite(Number(value)) ? Number(value) : 0));
  }, 0);

  // Keep low-volume charts honest: fractional tick positions rounded for display
  // produce duplicate labels such as 1, 1 and 2, 2. Scale larger charts with
  // integer "nice" steps, never fractional units.
  const roughStep = Math.max(1, max / 5);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalized = roughStep / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = Math.max(1, nice * magnitude);
  const upper = Math.max(step, Math.ceil(max / step) * step);
  const ticks = [];
  for (let value = 0; value <= upper; value += step) ticks.push(value);
  return { upper, ticks };
}

export function ForecastChart({ historyPoints, futurePoints, rangePoints, stockoutTime, reorderTime, poLandsTime = null, intervalWidth, upcomingPromos, holidayWindows = [], lm, todaySales = null, closedDays = [] }) {
  /* The strip's labels are laid out in pixels, so it has to know how wide it
     actually is — percent-only layout is what let labels overrun each other and
     the card edge. Hooks run before any early return. */
  const roRef = React.useRef(null);
  const [chartW, setChartW] = React.useState(0);
  /* The chart's own width, so the date tags above the plot can be laid out in pixels
     and kept from touching. A callback ref measures whenever the node mounts. */
  const chartRef = React.useCallback((node) => {
    if (roRef.current) { roRef.current.disconnect(); roRef.current = null; }
    if (!node) return;
    setChartW(node.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setChartW(node.clientWidth));
    ro.observe(node);
    roRef.current = ro;
  }, []);
  React.useEffect(() => () => roRef.current?.disconnect(), []);
  /* The window the chart shows (indexes into `data`), set by dragging the slider under
     it. Null until moved; reset whenever the data itself changes (see winKey). */
  const [win, setWin] = React.useState(null);

  /* Built once per change of inputs, not on every render: the chart resets its zoom
     window whenever it is handed a new data array, which undid the window below. */
  const _sum = (arr, f) => (arr || []).reduce((t, p) => t + (Number(f(p)) || 0), 0);
  const _ends = arr => arr && arr.length ? `${arr.length}:${arr[0].x}:${arr[arr.length - 1].x}` : "0";
  // The parent rebuilds these arrays on every render, so the memo is keyed on content.
  const dataSig = [_ends(historyPoints), _sum(historyPoints, p => p.y), _ends(futurePoints),
    _sum(futurePoints, p => p.y), _sum(rangePoints, p => p.y?.[1]),
    todaySales ? `${todaySales.day}:${todaySales.units}:${todaySales.stale}` : ""].join("|");
  const data = React.useMemo(() => {
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
    return Array.from(dataMap.values()).sort((a, b) => a.x - b.x);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig]);

  if (!data) return null;

  const yAxis = wholeNumberAxis(data);

  /* WHAT THE CHART OPENS ON. The whole history used to fill the chart, so three years
     of sales pushed the forecast (the part being planned from) into a sliver at the
     right edge. It now opens on recent history plus the whole forecast: history 1.5x
     as long as the forecast, between 4 months and a year of it. The slider under the
     chart still reaches back to the first day. */
  const DAY = 864e5;
  const lastHistX = historyPoints.length ? historyPoints[historyPoints.length - 1].x : (data[0]?.x ?? 0);
  const lastX = data.length ? data[data.length - 1].x : lastHistX;
  const fcDays = Math.max(0, (lastX - lastHistX) / DAY);
  const histDays = Math.min(365, Math.max(120, fcDays * 1.5));
  const defStart = Math.max(0, data.findIndex(d => d.x >= lastHistX - histDays * DAY));
  const winKey = `${data.length}|${data[0]?.x}|${lastX}`;
  const w = (win && win.key === winKey) ? win
    : { key: winKey, start: defStart, end: Math.max(0, data.length - 1) };
  const onBrush = (r) => {
    if (r && Number.isFinite(r.startIndex) && Number.isFinite(r.endIndex))
      setWin({ key: winKey, start: r.startIndex, end: r.endIndex });
  };

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

  /* THE VISIBLE WINDOW, so every marker sits over its own date when zoomed. */
  const AX_L = 36, AX_R = 16;
  const x0 = data.length ? data[Math.min(w.start, data.length - 1)].x : 0;
  const x1 = data.length ? data[Math.min(w.end, data.length - 1)].x : 1;
  const span = Math.max(1, x1 - x0);

  /* About seven evenly spaced date labels across what's SHOWN: month starts ("Mar '26")
     for a long view, weeks for a short one. */
  const spanDays = span / DAY;
  const ticks = [];
  if (spanDays > 100) {
    const months = spanDays / 30.4;
    const step = [1, 2, 3, 4, 6, 12].find(k => months / k <= 7) || 12;
    const d0 = new Date(x0); let t = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth() + 1, 1);
    while (t <= x1) {
      const dd = new Date(t);
      if (dd.getUTCMonth() % step === 0) ticks.push(t);
      t = Date.UTC(dd.getUTCFullYear(), dd.getUTCMonth() + 1, 1);
    }
  } else {
    const stepD = Math.max(1, Math.ceil(spanDays / 7 / 7)) * 7;
    for (let t = x0 + DAY; t <= x1; t += stepD * DAY) ticks.push(t);
  }
  const fmtTick = spanDays > 100
    ? (ms) => `${new Date(ms).toLocaleDateString("en-US", { month: "short", timeZone: "UTC" })} '${String(new Date(ms).getUTCFullYear()).slice(2)}`
    : fmtDate;

  /* KEY DATES, drawn once, inside the chart. These used to appear twice: in a strip
     above the chart (labels, slanted leader lines, a dashed bracket and a caption) and
     again as small labels inside the plot. Close together (an order date the day before
     today) they piled into each other. Now each date is one line and one small tag at
     the top of the plot. Tags that would touch move to a second row. */
  const W = (chartW || 640) - AX_L - AX_R;           // plot width in pixels
  const pxOf = t => ((t - x0) / span) * W;
  const MARK_H = 20, TAG_H = 16, CHAR = 6.6, PADX = 7, GAP = 6;
  const markers = [
    { t: todayMs(),  name: "Today",    color: CT.soft, dash: "2 3", width: 1 },
    reorderTime  && { t: reorderTime,  name: "Order by", color: CT.blue, dash: "5 4", width: 1.5 },
    poLandsTime  && { t: poLandsTime,  name: "PO lands", color: CT.blue, dash: "2 3", width: 1.5 },
    stockoutTime && { t: stockoutTime, name: "Runs out", color: CT.red,  dash: "5 4", width: 1.5 },
  ].filter(Boolean).filter(m => m.t >= x0 && m.t <= x1)
   .sort((a, b) => a.t - b.t)
   .map(m => {
     const text = `${m.name} · ${fmtDate(m.t)}`;
     return { ...m, text, tw: text.length * CHAR + PADX * 2, px: pxOf(m.t) };
   });
  // Greedy rows: each tag centred on its line, kept inside the plot, and moved down a
  // row when it would touch the tag before it. Three rows at most.
  const placed = [];
  markers.forEach(m => {
    const left = Math.min(Math.max(m.px - m.tw / 2, 0), Math.max(0, W - m.tw));
    let row = 0;
    while (row < 2 && placed.some(p => p.row === row && left < p.left + p.tw + GAP && p.left < left + m.tw + GAP)) row++;
    placed.push({ ...m, left, row });
  });
  const rows = placed.length ? Math.max(...placed.map(p => p.row)) + 1 : 0;
  const TOP = 10 + rows * MARK_H;                      // room above the plot for the tags

  const MarkerTag = ({ viewBox, m }) => {
    if (!viewBox) return null;
    const ty = viewBox.y - TOP + 6 + m.row * MARK_H;   // this tag's row, above the plot
    const tx = AX_L + m.left;
    return (
      <g style={{ pointerEvents: "none" }}>
        <line x1={viewBox.x} y1={ty + TAG_H} x2={viewBox.x} y2={viewBox.y}
          stroke={m.color} strokeWidth={1} opacity={0.5} />
        <rect x={tx} y={ty} width={m.tw} height={TAG_H} rx={3}
          fill={CT.panel} stroke={m.color} strokeOpacity={0.55} strokeWidth={1} />
        <text x={tx + m.tw / 2} y={ty + TAG_H / 2 + 0.5} textAnchor="middle" dominantBaseline="middle"
          fontSize={11} fontFamily={MONO} fill={m.color}>{m.text}</text>
      </g>
    );
  };

  /* Lead time: the stretch between the last day to order and the day stock runs out.
     A faint band, named inside it only when there is room for the words. */
  const leadGap = (reorderTime && stockoutTime && stockoutTime > reorderTime)
    ? { a: Math.max(reorderTime, x0), b: Math.min(stockoutTime, x1),
        days: Math.round((stockoutTime - reorderTime) / 864e5) } : null;
  const leadText = leadGap ? `${leadGap.days}-day lead time` : "";
  const leadFits = leadGap && leadGap.b > leadGap.a && (pxOf(leadGap.b) - pxOf(leadGap.a)) > leadText.length * 6.2 + 12;

  return (
    <div ref={chartRef} style={{ height: "100%", width: "100%" }}>
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: TOP, right: AX_R, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={grid} vertical={false} />
        <XAxis dataKey="x" type="number" scale="time" domain={[x0, x1]} allowDataOverflow ticks={ticks} tickFormatter={fmtTick}
          tick={{ fill: tick, fontSize: 11, fontFamily: MONO }} axisLine={{ stroke: grid }} tickLine={{ stroke: grid }} />
        <YAxis domain={[0, yAxis.upper]} ticks={yAxis.ticks} tickFormatter={v => v.toLocaleString()}
          tick={{ fill: tick, fontSize: 11, fontFamily: MONO }} axisLine={false} tickLine={false} width={AX_L} />
        <Tooltip content={<ChartTooltip lm={lm} />} />
        <Legend wrapperStyle={{ fontSize: 10, fontFamily: "ui-monospace, monospace", color: legendColor, paddingBottom: 4 }} />

        {/* Holidays the forecast shapes, shaded grey on their own dates and named at the
            top of the band: unnamed, a holiday overlapping the lead time read as part of it
            (or as the coverage period). Day spans run to the end of the last day. */}
        {(holidayWindows || []).map((h, i) => {
          const a = new Date(h.start + "T00:00:00Z").getTime(), b = new Date(h.end + "T00:00:00Z").getTime() + 86400000;
          // Named only when the band is wide enough for its name (the lead-time label's
          // rule), so neighbouring holidays never print over each other.
          const fits = (pxOf(Math.min(b, x1)) - pxOf(Math.max(a, x0))) > String(h.name).length * 6.2 + 12;
          return (
            <ReferenceArea key={`h${i}`} ifOverflow="hidden" x1={a} x2={b}
              fill={CT.ink} fillOpacity={lm ? 0.05 : 0.06}
              label={fits ? { value: h.name, position: "insideTopLeft", fill: CT.dim, fontSize: 10.5, fontFamily: MONO, offset: 6 } : undefined} />
          );
        })}
        {(upcomingPromos || []).map((p, i) => (
          <ReferenceArea key={i} ifOverflow="hidden" x1={new Date(p.date + "T00:00:00Z").getTime()} x2={new Date((p.end_date || p.date) + "T00:00:00Z").getTime()}
            fill={CT.blue} fillOpacity={lm ? 0.12 : 0.10} label={{ value: p.label || "Promo", fill: CT.blue, fontSize: 11 }} />
        ))}
        {leadGap && leadGap.b > leadGap.a && (
          <ReferenceArea x1={leadGap.a} x2={leadGap.b} ifOverflow="hidden" fill={CT.red} fillOpacity={lm ? 0.06 : 0.07}
            label={leadFits ? { value: leadText, position: "insideBottom", fill: CT.red, fontSize: 10.5, fontFamily: MONO, offset: 8 } : undefined} />
        )}

        <Area name={ciLabel} dataKey="rangeHigh" stroke="none" fill={CT.amber} fillOpacity={lm ? 0.14 : 0.16} legendType="square" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="_rangeLow" dataKey="rangeLow" stroke="none" fill={bandMask} fillOpacity={1} legendType="none" isAnimationActive={false} connectNulls dot={false} activeDot={false} />
        <Area name="Historical Sales" dataKey="y" stroke={CT.ink} strokeWidth={1.6} fill={CT.ink} fillOpacity={lm ? 0.07 : 0.05} isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: CT.ink }} />
        <Line name="Forecast" dataKey="forecast" stroke={CT.amber} strokeWidth={2} strokeDasharray="5 4" isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3, fill: CT.amber }} />
        {/* A ring, not a filled dot, and no connecting line: it is a partial reading. */}
        <Line name="Today so far" dataKey="todaySoFar" stroke="none" isAnimationActive={false}
          legendType="circle" connectNulls={false}
          dot={{ r: 5, stroke: CT.ink, strokeWidth: 2, fill: CT.panel }}
          activeDot={{ r: 6, stroke: CT.ink, strokeWidth: 2, fill: CT.panel }} />

        {/* Days the store is closed: an orange dot on the axis. Nothing sold, nothing is
            learned from them and nothing is forecast for them. */}
        {(closedDays || []).filter(t => t >= x0 && t <= x1).map(t => (
          <ReferenceDot key={`c${t}`} x={t} y={0} r={3.5} ifOverflow="visible" isFront
            fill={CLOSED_ORANGE} stroke={CT.panel} strokeWidth={1}
            shape={(p) => (
              <circle cx={p.cx} cy={p.cy} r={3.5} fill={CLOSED_ORANGE} stroke={CT.panel} strokeWidth={1}>
                <title>{`Store closed · ${new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })}`}</title>
              </circle>
            )} />
        ))}

        {placed.map((m, i) => (
          <ReferenceLine key={`m${i}`} x={m.t} stroke={m.color} strokeWidth={m.width} strokeDasharray={m.dash}
            ifOverflow="hidden" label={<MarkerTag m={m} />} />
        ))}

        {/* No date text on the slider handles: it printed half outside the chart ("Ja")
            and the axis above already carries the dates. */}
        <Brush dataKey="x" tickFormatter={() => ""} height={20} stroke={CT.line2} fill={CT.sunken} travellerWidth={6}
          startIndex={w.start} endIndex={w.end} onChange={onBrush}
          style={{ fontSize: 8, fontFamily: "ui-monospace, monospace" }} />
      </ComposedChart>
    </ResponsiveContainer>
    </div>
  );
}

export default ForecastChart;
