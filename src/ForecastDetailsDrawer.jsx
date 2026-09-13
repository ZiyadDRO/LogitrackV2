// ─────────────────────────────────────────────────────────────────────────
// ForecastDetailsDrawer.jsx
// A self-contained "why this forecast?" side panel for the statsforecast engine.
//
// Renders a small vertical tab pinned to the right edge of the screen; clicking
// it slides in a panel that explains, in plain language, how the forecast was
// produced: data-quality classification, which model was chosen and why, what
// it accounts for (seasonality / price), how consistent demand is, and the
// candidate models that were backtested.
//
// Reads `data.forecastDetails`, which the new engine returns from /api/forecast.
// If that field is absent (e.g. you're running the old Prophet backend), the
// panel degrades gracefully to a short note instead of breaking.
//
// Integration (already wired if you used the provided App.jsx edit):
//   import ForecastDetailsDrawer from './ForecastDetailsDrawer';
//   ...inside the SKU detail view, where `mlData` and `lm` exist:
//   <ForecastDetailsDrawer data={mlData} lm={lm} />
// ─────────────────────────────────────────────────────────────────────────
import React from 'react';

function Section({ title, children, lm }) {
  return (
    <div className="mb-5">
      <div className={`text-[14px] font-semibold uppercase tracking-widest mb-2 ${lm ? 'text-[var(--t-dim)]' : 'text-[var(--t-dim)]'}`}>
        {title}
      </div>
      {children}
    </div>
  );
}

export default function ForecastDetailsDrawer({ data, lm = false, open = false, onClose = () => {} }) {
  const d = data?.forecastDetails || null;

  // Theming tokens (mirror the rest of the app)
  const panelBg  = lm ? 'bg-[var(--t-panel)]' : 'bg-[#0d1117]';
  const border   = lm ? 'border-[var(--t-line)]' : 'border-[var(--t-line)]';
  const textMain = lm ? 'text-[var(--t-ink)]' : 'text-[var(--t-ink)]';
  const textBody = lm ? 'text-[var(--t-soft)]' : 'text-[var(--t-soft)]';
  const textMute = lm ? 'text-[var(--t-dim)]' : 'text-[var(--t-dim)]';
  const cardBg   = lm ? 'bg-[var(--t-sunken)] border-[var(--t-line)]' : 'bg-[#0a0f16] border-[var(--t-line)]';

  return (
    <>
      {!open ? null : (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" onClick={onClose} />
          <aside className={`absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto border-l ${border} ${panelBg} shadow-2xl`}>
            {/* Header */}
            <div className={`sticky top-0 z-10 flex items-center justify-between px-5 py-4 border-b ${border} ${panelBg}`}>
              <div>
                <div className={`text-[16.5px] font-bold ${textMain}`}>How this forecast was made</div>
                <div className={`text-[14px] ${textMute}`}>{data?.skuName || data?.skuId || ''}</div>
              </div>
              <button
                onClick={onClose}
                className={`rounded-lg px-2 py-1 text-lg leading-none ${lm ? 'text-[var(--t-dim)] hover:text-[var(--t-soft)]' : 'text-[var(--t-dim)] hover:text-white'}`}
              >×</button>
            </div>

            <div className="px-5 py-5">
              {!d ? (
                <div className={`rounded-xl border ${cardBg} p-4 text-[15px] leading-relaxed ${textBody}`}>
                  Detailed model breakdown is available when the dashboard is connected to the
                  <span className="font-semibold"> statsforecast engine</span>. The current backend didn’t return
                  these details (you may be running the original Prophet model).
                </div>
              ) : (
                <>
                  {/* Last-resort caution banner */}
                  {d.caution && (
                    <div className={`mb-4 rounded-xl border px-3 py-2.5 text-[15px] leading-relaxed
                      ${lm ? 'bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]' : 'bg-[var(--t-warn-soft)] text-[var(--t-warn)] border-[var(--t-warn-line)]'}`}>
                      <span className="font-semibold">⚠ {d.caution}</span>
                    </div>
                  )}

                  {/* Model used */}
                  <Section title="Model used" lm={lm}>
                    <div className={`rounded-xl border ${cardBg} p-4`}>
                      {d.routeLabel && (
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 mb-2 text-[14px] font-semibold uppercase tracking-wide
                          ${lm ? 'bg-[var(--t-info-soft)] text-[var(--t-info)] border-[var(--t-info-line)]' : 'bg-[var(--t-info-soft)] text-[var(--t-info)] border-[var(--t-info-line)]'}`}>
                          {d.routeLabel}
                        </span>
                      )}
                      <div className={`text-base font-bold ${textMain}`}>{d.model?.label}</div>
                      <div className={`mt-1 text-[15px] ${textBody} leading-relaxed`}>{d.whyChosen}</div>
                    </div>
                  </Section>

                  {/* Lifecycle status — baseline / young / discontinued */}
                  {(data.inactive || data.tooNew || data.young) && (
                    <Section title="Lifecycle status" lm={lm}>
                      <div className={`rounded-xl border ${cardBg} p-4 text-[15px] ${textBody} leading-relaxed`}>
                        {data.inactive
                          ? <><span className="font-semibold">Discontinued / dormant.</span> {data.inactiveMessage}</>
                          : data.tooNew
                          ? <><span className="font-semibold">Establishing baseline — {data.ownDays}/{data.baselineDays} days.</span> {data.tooNewMessage}{data.observedRunwayDays != null ? ` At ~${data.observedDailyRate}/day, about ${data.observedRunwayDays} days of stock left.` : ''}</>
                          : <><span className="font-semibold">Young / provisional — {data.ownDays}/{data.youngThreshold} days.</span> {data.youngMessage}</>}
                      </div>
                    </Section>
                  )}

                  {/* Behavioural peer group — which products it pools from and why */}
                  {data.clusterInfo && (
                    <Section title="Behavioural peer group" lm={lm}>
                      <div className={`rounded-xl border ${cardBg} p-4`}>
                        <p className={`text-[15px] ${textBody} leading-relaxed`}>
                          {data.route === 'global'
                            ? <>It borrows its seasonal shape from {data.clusterInfo.chosen?.length} behaviourally-similar product{data.clusterInfo.chosen?.length !== 1 ? 's' : ''}: <span className="font-semibold">{(data.clusterInfo.chosen || []).join(', ')}</span>. They were grouped because their weekly/seasonal patterns line up, not just because they share a category label.</>
                            : <>Within its category it clusters with: <span className="font-semibold">{(data.clusterInfo.chosen || []).join(', ')}</span>.</>}
                        </p>
                        <p className={`mt-1.5 text-[14px] ${textMute}`}>Matched by: {data.clusterInfo.basis}.</p>
                        {data.clusterInfo.clusters?.length > 1 && (
                          <p className={`mt-1.5 text-[14px] ${textMute}`}>This category splits into {data.clusterInfo.clusters.length} behavioural groups — only the matching one is pooled; the others behave differently and were left out.</p>
                        )}
                      </div>
                    </Section>
                  )}

                  {/* Data quality */}
                  <Section title="Data quality" lm={lm}>
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[14px] font-semibold
                        ${lm ? 'bg-[var(--t-info-soft)] text-[var(--t-info)] border-[var(--t-info-line)]' : 'bg-[var(--t-info-soft)] text-[var(--t-info)] border-[var(--t-info-line)]'}`}>
                        {d.demand?.label}
                      </span>
                      <span className={`text-[14px] ${textMute}`}>{d.dataPoints?.toLocaleString()} days with sales</span>
                    </div>
                    <p className={`text-[15px] ${textBody} leading-relaxed`}>{d.demand?.description}</p>
                  </Section>

                  {/* What it accounts for */}
                  <Section title="What it accounts for" lm={lm}>
                    <ul className="space-y-2">
                      <li className={`text-[15px] ${textBody} leading-relaxed flex gap-2`}>
                        <span className="text-[var(--t-info)]">•</span><span>{d.seasonality?.text}</span>
                      </li>
                      <li className={`text-[15px] ${textBody} leading-relaxed flex gap-2`}>
                        <span className={d.price?.used ? 'text-[var(--t-good)]' : 'text-[var(--t-dim)]'}>•</span>
                        <span>{d.price?.text}</span>
                      </li>
                    </ul>
                  </Section>

                  {/* Consistency */}
                  <Section title="Demand consistency" lm={lm}>
                    <p className={`text-[15px] ${textBody} leading-relaxed`}>{d.consistency?.text}</p>
                  </Section>

                  {/* Forecast range (conformal band) */}
                  {d.interval && (
                    <Section title="Forecast range" lm={lm}>
                      <div className={`rounded-xl border ${cardBg} p-4`}>
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[14px] font-semibold
                            ${lm ? 'bg-[var(--t-accent-soft)] text-[var(--t-accent)] border-[var(--t-accent-line)]' : 'bg-[var(--t-accent-soft)] text-[var(--t-accent)] border-[var(--t-accent-line)]'}`}>
                            {d.interval.level}% band
                          </span>
                          {d.interval.method === 'conformal' && (
                            <span className={`text-[14px] ${textMute}`}>empirically calibrated</span>
                          )}
                        </div>
                        <p className={`text-[15px] ${textBody} leading-relaxed`}>{d.interval.text}</p>
                      </div>
                    </Section>
                  )}

                  {/* Candidates tested */}
                  {Array.isArray(d.candidates) && d.candidates.length > 0 && (
                    <Section title={`Models tested (${d.candidates.length})`} lm={lm}>
                      <div className={`rounded-xl border ${cardBg} divide-y ${lm ? 'divide-[var(--t-line)]' : 'divide-[var(--t-line)]'}`}>
                        {d.candidates.map((c, i) => (
                          <div key={i} className="flex items-center justify-between px-3 py-2">
                            <div className="flex items-center gap-2">
                              {c.chosen && (
                                <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[13px] font-bold uppercase tracking-wide
                                  ${lm ? 'bg-[var(--t-good-soft)] text-[var(--t-good)]' : 'bg-[var(--t-good-soft)] text-[var(--t-good)]'}`}>Chosen</span>
                              )}
                              <span className={`text-[15px] ${c.chosen ? `font-semibold ${textMain}` : textBody}`}>{c.label}</span>
                            </div>
                            <span className={`font-mono text-[14px] ${textMute}`}>
                              {c.mae == null ? '—' : `MAE ${c.mae}`}
                            </span>
                          </div>
                        ))}
                      </div>
                      <p className={`mt-1.5 text-[14px] ${textMute}`}>{d.backtestText} Lower MAE = more accurate.</p>
                    </Section>
                  )}
                </>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
