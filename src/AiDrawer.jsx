import { buildScorecardBody, todayStr } from './lib/helpers';
import { shownForecasts, pickDetailSkus, buildFleetContext, fitToBudget, reorderPhrase } from './lib/aiContext';
import { useState, useRef, useEffect } from "react";
import { loadStorage, saveStorage } from "./lib/storage";
import { GROQ_MODEL_CHAT, groqChat,
         checkGroqModels, groqHealthMessage, usageSummary } from "./lib/ai";

// The context the assistant gets (what's showing, live store data, a roster of every
// product, full detail for the ones asked about) is built in lib/aiContext.js.

// Was a third private copy of this, and the most wrong: it sent the raw
// params.leadTime AND dropped `fees`, so the model was briefed with different
// statuses and different margins than either tab showed. Uses the shared builder
// in helpers now.

// ─────────────────────────────────────────────
// REORDER BRIEF
// ─────────────────────────────────────────────
function ReorderBrief({ skuForecasts, openPOs, skuParams, skuList, apiKey }) {
  const [brief,   setBrief]   = useState(null);
  const [loading, setLoading] = useState(false);
  const [copied,  setCopied]  = useState(false);

  const generate = async () => {
    setLoading(true);
    setBrief(null);

    const DEFAULT_PARAMS = { stock: 500, leadTime: 14, coverage: 30, strategy: "balanced" };
    const today = new Date();
    const fmt = d => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

    // Only the products showing (the drawer passes them already scoped). Stock nobody
    // counted has no reorder date or quantity; the brief says so instead of "OVERDUE".
    const skuLines = (skuForecasts || []).map(s => {
      const po     = openPOs?.[s.skuId];
      const params = skuParams?.[s.skuId] ?? DEFAULT_PARAMS;
      const counted = s.stockCounted !== false;
      return [
        `SKU: ${s.skuName} (${s.skuId})`,
        `  Reorder: ${po ? "ON ORDER, alert suppressed" : reorderPhrase(s)}`,
        counted ? `  Units to order: ${s.orderQty?.toLocaleString()}` : "  Units to order: unknown until the stock is counted",
        `  Stockout: ${!counted ? "unknown (stock not counted)" : s.alreadyOut ? "out of stock now" : s.daysUntilStockout == null ? "not within the forecast" : `in ${s.daysUntilStockout} days`}`,
        `  Lead time: ${params.leadTime} days`,
        po ? `  Open PO: ${po.qty} units from ${po.supplier || "supplier"}, arriving ${po.delivery}` : "",
      ].filter(Boolean).join("\n");
    }).join("\n\n");

    const prompt = `You are a purchasing manager writing a weekly procurement brief.
Today is ${fmt(today)}.

Write a concise, professional reorder brief that:
1. Opens with one sentence summarizing the overall fleet status
2. Lists each SKU that needs action, in order of urgency (overdue first, then by days remaining)
3. For each actionable SKU: state what to order, how many units, and by when — in one clear sentence
4. Lists SKUs with open POs as "covered" in a separate short section
5. Lists SKUs needing no action as "healthy" in one line, and SKUs whose stock was never counted as "count needed"
6. Closes with one sentence on the biggest risk if any deadline is missed

Use plain text. No markdown, no asterisks. Use • for bullet points. Never use em dashes or en dashes.
Write it so it could be forwarded directly to a procurement team.

Fleet data:
${skuLines}`;

    try {
      // Same model chain and waiting as the chat (lib/ai.js groqChat).
      const r = await groqChat({ messages: [{ role: "user", content: prompt }], max_tokens: 600,
                                 apiKey, onWait: secs => setBrief(`Free AI allowance busy, retrying in ${secs}s…`) });
      setBrief(r.text || "Could not generate brief.");
    } catch (e) {
      setBrief(e?.message || "Failed to generate brief. Check your API key.");
    } finally {
      setLoading(false);
    }
  };

  const copy = () => {
    navigator.clipboard.writeText(brief);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mx-4 mb-3 border border-[var(--t-line)] rounded-2xl overflow-hidden">
      <button onClick={generate} disabled={loading}
        className="w-full flex items-center gap-2.5 px-4 py-3 bg-[var(--t-bg)] hover:bg-[var(--t-line)] disabled:opacity-60 transition-colors text-left">
        <div className="h-6 w-6 rounded-lg bg-[var(--t-info-soft)] border border-[var(--t-info-line)] flex items-center justify-center shrink-0">
          {loading
            ? <div className="h-2.5 w-2.5 border-2 border-[var(--t-info-line)] border-t-transparent rounded-full animate-spin" />
            : <svg className="h-3 w-3 text-[var(--t-info)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
          }
        </div>
        <div>
          <div className="text-[14px] font-semibold text-[var(--t-ink)]">
            {loading ? "Generating brief…" : "Generate Reorder Brief"}
          </div>
          <div className="text-[13px] text-[var(--t-dim)]">AI-written purchasing summary, ready to forward</div>
        </div>
      </button>
      {brief && (
        <div className="border-t border-[var(--t-line)] bg-[#070b10]">
          <div className="px-4 py-3 max-h-64 overflow-y-auto">
            <p className="text-[14px] text-[var(--t-soft)] leading-relaxed whitespace-pre-wrap">{brief}</p>
          </div>
          <div className="px-4 py-2 border-t border-[var(--t-line)] flex justify-between items-center">
            <button onClick={() => setBrief(null)} className="text-[13px] text-[var(--t-soft)] hover:text-[var(--t-dim)] transition-colors">dismiss</button>
            <button onClick={copy}
              className="flex items-center gap-1.5 text-[13px] font-semibold text-[var(--t-info)] hover:text-[var(--t-info)] border border-[var(--t-info-line)] hover:border-[var(--t-info-line)] bg-[var(--t-info-soft)] rounded-lg px-2.5 py-1 transition-all">
              {copied
                ? <><svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>Copied!</>
                : <><svg className="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>Copy to clipboard</>
              }
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


const SUGGESTED_PROMPTS = [
  "Which SKUs are unhealthiest right now and why?",
  "Any slow movers that are still profitable I shouldn't liquidate?",
  "Which SKUs are overstocked or look like dead stock?",
  "Where is my inventory cash tied up with the lowest return?",
];

// ─────────────────────────────────────────────
// MESSAGE BUBBLE
// ─────────────────────────────────────────────
function MessageBubble({ msg }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      {!isUser && (
        <div className="h-6 w-6 shrink-0 rounded-lg bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center mt-0.5">
          <svg className="h-3 w-3 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        </div>
      )}
      <div className={`max-w-[85%] rounded-2xl px-3.5 py-2.5 text-[14px] leading-relaxed whitespace-pre-wrap ${
        isUser
          ? "bg-[var(--t-info-soft)] border border-[var(--t-info-line)] text-[var(--t-info)] rounded-tr-sm"
          : "bg-[var(--t-line)] border border-[var(--t-line2)] text-[var(--t-ink)] rounded-tl-sm"
      }`}>
        {msg.content}
        {/* Answered by a backup model because the main one was at its free limit. Said
            quietly, since the smaller model's answers can be a little thinner. */}
        {msg.model && (
          <div className="mt-1.5 text-[12px] text-[var(--t-dim)]">
            answered by {msg.model.split("/").pop()} (backup, main model was at its free limit)
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// TYPING INDICATOR
// ─────────────────────────────────────────────
function TypingIndicator() {
  return (
    <div className="flex gap-2.5 flex-row">
      <div className="h-6 w-6 shrink-0 rounded-lg bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center">
        <svg className="h-3 w-3 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
      </div>
      <div className="bg-[var(--t-line)] border border-[var(--t-line2)] rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-1">
        {[0, 1, 2].map(i => (
          <div key={i} className="h-1.5 w-1.5 rounded-full bg-[var(--t-sunken)] animate-bounce"
               style={{ animationDelay: `${i * 150}ms`, animationDuration: "0.8s" }} />
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// MAIN DRAWER
// ─────────────────────────────────────────────
export default function AiDrawer({ skuForecasts, openPOs, skuParams, skuList, folders, apiKey, suppliers, api = "http://localhost:8000" }) {
  const [open,     setOpen]     = useState(false);
  const [messages, setMessages] = useState([]);
  const [input,    setInput]    = useState("");
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState(null);
  // Off by default: including full sales history makes the request too large for
  // Groq's free tier (12k tokens/min). Turn on once you're on a higher tier.
  const [includeHistory, setIncludeHistory] = useState(false);
  // Deprecation banner and a live token readout. Both exist because this project's
  // failures have all been silent: a retired model surfaced as "couldn't parse that",
  // and an oversized request surfaced as nothing at all.
  const [health, setHealth] = useState(null);
  const [usage,  setUsage]  = useState(null);
  // "Free allowance busy, retrying in 12s…" while groqChat waits out a per-minute limit.
  const [waitNote, setWaitNote] = useState(null);
  // The last backtest's headline numbers, kept until a newer run finishes.
  const btCache = useRef({ ranAt: 0, data: null });

  useEffect(() => {
    let alive = true;
    checkGroqModels().then(h => { if (alive) setHealth(h); });
    return () => { alive = false; };
  }, []);
  const bottomRef = useRef(null);
  const inputRef  = useRef(null);

  // ── Draggable button state ──
  //
  // null means "no saved position", which renders from the CSS bottom instead. Anything
  // that is not a finite number MUST collapse back to null, because `top: NaNpx` is an
  // invalid declaration: the browser drops it, `bottom` is already "auto", and the
  // button lands at viewport y=0 with no way to move it. `NaN ?? fallback` returns NaN
  // rather than the fallback, so every later drag computed NaN too and the button was
  // stuck at the top of the screen permanently.
  //
  // It got there from a plain CLICK. mouseup fired without any movement and saved
  // String(btnYRef.current) while btnY was still null, writing the literal "null", which
  // parseInt turned into NaN on the next load. So opening the drawer once and reloading
  // was enough to break it.
  const readBtnY = () => {
    const n = parseInt(loadStorage("logitrack_ai_btn_y", null), 10);
    return Number.isFinite(n) ? n : null;
  };
  const clampBtnY = (n) =>
    (Number.isFinite(n) ? Math.max(8, Math.min(window.innerHeight - 60, n)) : null);
  const [btnY, setBtnY] = useState(() => clampBtnY(readBtnY()));
  const btnYRef      = useRef(btnY);
  const hasMoved     = useRef(false);
  const dragStartY   = useRef(0);
  const dragStartTop = useRef(0);

  useEffect(() => { btnYRef.current = btnY; }, [btnY]);

  // A position saved on a tall window is off-screen on a short one. The clamp only ever
  // ran mid-drag, so a smaller window left the button below the fold with no way to
  // reach it — the same class of bug as the NaN, just at the other end.
  useEffect(() => {
    const onResize = () => setBtnY(prev => (prev === null ? null : clampBtnY(prev)));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const handleMouseDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    hasMoved.current     = false;
    dragStartY.current   = e.clientY;
    // Number.isFinite, not ??, so a bad stored value can't poison the drag.
    dragStartTop.current = Number.isFinite(btnYRef.current)
      ? btnYRef.current
      : (window.innerHeight - 24 - 44);

    const onMove = (ev) => {
      const delta = ev.clientY - dragStartY.current;
      if (Math.abs(delta) > 5) hasMoved.current = true;
      const newTop = Math.max(8, Math.min(window.innerHeight - 60, dragStartTop.current + delta));
      setBtnY(newTop);
    };

    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      // Only persist an actual drag, and only a real number. Saving on every click is
      // what wrote "null" into storage in the first place.
      if (hasMoved.current && Number.isFinite(btnYRef.current)) {
        saveStorage("logitrack_ai_btn_y", String(btnYRef.current));
      }
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const handleBtnClick = () => {
    if (hasMoved.current) return; // was a drag, not a click
    setOpen(true);
  };

  useEffect(() => {
    if (bottomRef.current) bottomRef.current.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  const sendMessage = async (text) => {
    const userText = (text ?? input).trim();
    if (!userText || loading) return;
    setInput("");
    setError(null);

    // Echo the user's message and show the thinking indicator IMMEDIATELY, before
    // any network calls. Previously setMessages ran only AFTER the scorecard and
    // groups fetches resolved, so the user's own message didn't appear for 1-2s
    // after hitting enter — it looked like nothing was happening.
    const newMessages = [...messages, { role: "user", content: userText }];
    setMessages(newMessages);
    setLoading(true);

    // Pull the AI's supporting context, all in parallel and all best-effort (a failure,
    // or an older backend without an endpoint, just leaves that part out):
    //   scorecard, groups       status, profit grade, grouping and family borrowing
    //   workspace, connections  which store is showing and which are paused (names only)
    //   sync                    last nightly sync, last hourly stock reading
    //   closed-days             days the store is shut
    //   livelog                 live accuracy (limit=1: the summaries, not every entry)
    //   backtest                overall result, re-read only when a new run has finished
    //   history                 full sales history; opt-in (large)
    const fetchJsonSafe = async (url, opts) => {
      try { const r = await fetch(url, opts); return r.ok ? await r.json() : null; }
      catch { return null; }
    };
    const [scorecard, groups, history, workspace, connections, sync, closedDays, livelog, btStatus] = await Promise.all([
      fetchJsonSafe(`${api}/api/scorecard`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        /* `suppliers` is not optional. buildScorecardBody feeds it to planningLeadTime,
           which without it can't resolve a measured or supplier-inherited lead time and
           falls back to the raw params value. Dropping it here meant the assistant was
           briefed with different statuses than the Fleet and Scorecard tabs display. */
        body: JSON.stringify(buildScorecardBody(skuList, skuParams, openPOs, suppliers)),
      }),
      fetchJsonSafe(`${api}/api/groups`),
      includeHistory ? fetchJsonSafe(`${api}/api/history?recent_days=90`) : Promise.resolve(null),
      fetchJsonSafe(`${api}/api/workspace`),
      fetchJsonSafe(`${api}/api/connections`),
      fetchJsonSafe(`${api}/api/sync`),
      fetchJsonSafe(`${api}/api/closed-days`),
      fetchJsonSafe(`${api}/api/livelog?limit=1`),
      fetchJsonSafe(`${api}/api/backtest/status`),
    ]);
    // The backtest report is large, so it is fetched once per finished run.
    const ranAt = Number(btStatus?.lastRanAt || 0);
    if (ranAt && btCache.current.ranAt !== ranAt) {
      const full = await fetchJsonSafe(`${api}/api/backtest/last`);
      if (full?.available) {
        const r = full.result || {};
        // Only what the prompt uses: the headline and each product's own miss.
        const bySku = (r.bySku || []).map(x => ({ sku: x.sku, "WAPE%": x["WAPE%"], windows: x.windows, reportable: x.reportable }));
        btCache.current = { ranAt, data: { available: true, result: { ranAt: r.ranAt, overall: r.overall, tested: r.tested, bySku } } };
      }
    }
    const backtest = btStatus?.status === "running" && !btCache.current.data
      ? { available: false, job: btStatus } : btCache.current.data;

    // Only what the Fleet is showing: the loaded store's products (or the spreadsheets).
    const shown = shownForecasts(skuForecasts, skuList);
    // Which products get the full block. Everything else still appears in the roster.
    // The previous question is passed so a follow-up ("and when should I order it?")
    // keeps its subject.
    const prevQ = [...messages].reverse().find(m => m.role === "user")?.content || "";
    const detailFor = pickDetailSkus(userText, shown, scorecard, 3, prevQ);
    const live = { workspace, connections, sync, closedDays, livelog, backtest };
    // ~16,000 characters is ~4,500 tokens: with the instructions, the last few turns and
    // room for the reply, a request stays under the free tier's ~8,000-token ceiling.
    // Over budget, the history is cut first, then detail blocks from the end; the roster
    // of every product always survives.
    const fleetCtx = fitToBudget(buildFleetContext(shown, detailFor, {
      openPOs, skuParams, skuList, folders, suppliers, scorecard, groups, history, live,
    }), 16000);

    const storeName = workspace?.connectionId ? `the store "${workspace.label || "store"}"` : "the uploaded spreadsheets";
    const pausedNames = (connections?.connections || [])
      .filter(c => c.active === false && c.id !== workspace?.connectionId).map(c => `"${c.label}"`);

    // Every rule kept short: this rides along on every question, and on the free tier
    // every thousand tokens here is most of an extra question per minute.
    const systemPrompt = `You are the inventory assistant inside LogiTrack, a demand forecasting dashboard. You see exactly what the Fleet is showing right now: ${storeName}, ${shown.length} products. The data below has a one-line roster for every product and full detail for the ones the question is about. ${includeHistory
  ? "Sales history is included (monthly totals plus ~90 days daily); use it for past sales, trends and seasonality."
  : "Full sales history is not loaded (the 'Full sales history' toggle is off). If asked for older detail, say so and mention the toggle."}

Stores: only one store is loaded at a time.${pausedNames.length ? ` Paused: ${pausedNames.join(", ")}.` : ""} If asked about a paused store or its products, say that store is paused, so its data isn't loaded here, and that switching it to "In use" under Connect store loads it (which sets the current products aside). Never guess its numbers. A product not in the roster isn't among the products showing; say so. You can read live store data (today's sales so far, live prices and discounts, hourly stock readings) but cannot change anything in the store, place orders or edit settings.

Forecasts: each product is routed to Prophet (regular sellers), a pooled GLOBAL model (new or sparse products borrowing from similar ones), Croston/TSB (occasional sellers) or a placeholder; use the engine named. Holiday stretches are forecast from how past years sold around each holiday. One-off rushes (a spike that didn't happen the year before) are left out of the everyday level. Closed days forecast zero. "Out of season" means quiet the way it was this time last year: never call it dead stock. Today's live sales are not in the daily history until the nightly sync.

Stock and orders: stock nobody counted has no stockout date, reorder date or order quantity; say a count is needed rather than guessing. Dates assume ordering today (delivery = today + lead time); lost sales are not backordered.

Scorecard: STATUS (Healthy, Reorder due, Overstocked, Stockout risk, Dead stock) and PROFIT GRADE (A 40%+ margin, B 20%+, C under 20%, F a loss) are separate. Reorder due is routine. Slow A/B sellers get right-sized, not liquidated; only slow, overstocked C items are markdown candidates; F items need a price rise, cheaper cost or discontinuing. New products (under ~90 days) are never dead stock. Protection level comes from the backtest's cheapest tier, else the cost curve, else margin; the manager can override it.

Style: concise, specific, plain language, real numbers from the data. No markdown headers or asterisks; use • for lists. No em or en dashes. Folder questions get folder-level answers. Today is ${new Date(`${todayStr()}T00:00:00Z`).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" })}.

=== FLEET DATA ===
${fleetCtx}
=== END FLEET DATA ===`;

    // Only send the last few turns to the model (the big fleet context already
    // lives in the system prompt) so the request doesn't grow unbounded.
    // (newMessages / loading were already set at the top so the echo is instant.)
    const chatMessages = [
      { role: "system", content: systemPrompt },
      // The last two exchanges. Older replies are cut to their opening: each full reply
      // is up to ~700 tokens, and three of them on top of the fleet data could push a
      // request past the free tier's per-request ceiling.
      ...newMessages.slice(-5).map((m, i, arr) => ({
        role: m.role === "assistant" ? "assistant" : "user",
        content: m.role === "assistant" && i < arr.length - 2 && m.content.length > 900
          ? m.content.slice(0, 900) + " […]" : m.content,
      })),
    ];

    try {
      const r = await groqChat({ messages: chatMessages, max_tokens: 700, temperature: 0.3, apiKey,
                                 onWait: secs => setWaitNote(`Free AI allowance busy, retrying in ${secs}s…`) });
      setWaitNote(null);
      setUsage(usageSummary());
      setMessages(prev => [...prev, { role: "assistant", content: r.text || "No response received.",
                                      model: r.fallback ? r.model : null }]);
    } catch (err) {
      setWaitNote(null);
      const msg = err?.kind === "too_large"
        ? "That request is too big for the free tier" + (includeHistory
            ? ", almost certainly the full sales history. Turn off \"Full sales history\" below."
            : ". Ask about fewer products at once.")
        : (err?.message || "Something went wrong.");
      setError(msg);
      setMessages(prev => [...prev, { role: "assistant", content: msg }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  // Counted stock only: an uncounted product has no reorder date (null), and
  // `null <= 7` is true in JavaScript.
  const urgentCount = shownForecasts(skuForecasts, skuList).filter(s =>
    !openPOs?.[s.skuId] && s.stockCounted !== false && s.daysUntilReorder != null
    && s.daysUntilReorder <= 7
  ).length;

  // Build a concise folder count label for the header
  const folderCount = folders ? Object.keys(folders).length : 0;

  /* The same amber tint, laid over a solid panel so it's opaque: the tint alone is 8%
     alpha, which let the dashboard's card outlines show straight through the button. */
  const BTN_BG = "linear-gradient(var(--t-accent-soft), var(--t-accent-soft)), var(--t-panel)";

  return (
    <>
      {/* ── TRIGGER BUTTON (Y-axis draggable) ── */}
      <button
        onMouseDown={handleMouseDown}
        onClick={handleBtnClick}
        style={btnY !== null
          ? { position: "fixed", top: `${btnY}px`, right: "24px", bottom: "auto", zIndex: 40, background: BTN_BG }
          : { position: "fixed", bottom: "24px",  right: "24px",            zIndex: 40, background: BTN_BG }
        }
        className="flex items-center gap-2 px-4 py-2.5 rounded-2xl border border-[var(--t-accent-line)] shadow-lg shadow-[var(--t-accent-line)] transition-colors select-none cursor-grab active:cursor-grabbing"
      >
        {/* Grip indicator */}
        <svg className="h-3 w-3 text-[var(--t-accent)] shrink-0" viewBox="0 0 10 16" fill="currentColor">
          <circle cx="3" cy="3"  r="1.2"/><circle cx="7" cy="3"  r="1.2"/>
          <circle cx="3" cy="8"  r="1.2"/><circle cx="7" cy="8"  r="1.2"/>
          <circle cx="3" cy="13" r="1.2"/><circle cx="7" cy="13" r="1.2"/>
        </svg>
        <svg className="h-4 w-4 text-[var(--t-ink)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        <span className="text-[15px] font-semibold text-[var(--t-ink)]">Ask AI</span>
        {urgentCount > 0 && (
          <span className="h-4 w-4 rounded-full bg-[var(--t-bad-soft)] text-[var(--t-ink)] text-[13px] font-bold flex items-center justify-center">
            {urgentCount}
          </span>
        )}
      </button>

      {/* ── BACKDROP ── */}
      {open && (
        <div className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" onClick={() => setOpen(false)} />
      )}

      {/* ── DRAWER ── */}
      <div className={`fixed top-0 right-0 h-full z-50 w-[420px] flex flex-col bg-[#0a0e14] border-l border-[var(--t-line)] shadow-2xl shadow-black/60 transition-transform duration-300 ease-out ${open ? "translate-x-0" : "translate-x-full"}`}>

        {/* Header */}
        <div className="shrink-0 px-5 py-4 border-b border-[var(--t-line)] flex items-center gap-3">
          <div className="h-7 w-7 rounded-xl bg-[var(--t-accent-soft)] border border-[var(--t-accent-line)] flex items-center justify-center">
            <svg className="h-3.5 w-3.5 text-[var(--t-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
          </div>
          <div className="flex-1">
            <div className="text-[15px] font-bold text-[var(--t-ink)]">Fleet Intelligence</div>
            <div className="text-[13px] text-[var(--t-dim)] font-mono">
              {skuList?.length ?? 0} SKU{skuList?.length !== 1 ? "s" : ""}
              {folderCount > 0 ? ` · ${folderCount} folder${folderCount !== 1 ? "s" : ""}` : ""}
              {/* Derived from GROQ_MODEL_CHAT rather than typed, so the label can't outlive
                  the model it names the next time Groq retires one. */}
              {` · ${GROQ_MODEL_CHAT.split("/").pop()} + backups`}
              {/* The measured size of the last request. Every limit hit in this project
                  was diagnosed by estimating tokens from character counts, and the
                  estimates were wrong twice; this is the number Groq actually billed. */}
              {usage && (
                <span className={usage.tight ? "text-[var(--t-warn)]" : "text-[var(--t-soft)]"}>
                  {` · ${usage.text}`}{usage.tight ? " (tight)" : ""}
                </span>
              )}
            </div>
          </div>
          <button onClick={() => setOpen(false)}
            className="h-7 w-7 rounded-lg bg-[var(--t-line)] hover:bg-[var(--t-line)] flex items-center justify-center transition-colors">
            <svg className="h-3.5 w-3.5 text-[var(--t-dim)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Reorder Brief */}
        <ReorderBrief
          skuForecasts={shownForecasts(skuForecasts, skuList)}
          openPOs={openPOs}
          skuParams={skuParams}
          skuList={skuList}
          apiKey={apiKey}
        />

        {/* Retired-model banner. Sits above everything because when it fires, nothing
            else in this drawer will work, and the alternative is six features failing
            separately with errors that each describe a symptom rather than the cause. */}
        {groqHealthMessage(health) && (
          <div className="shrink-0 mx-4 mt-3 rounded-lg border border-[var(--t-warn-line)] bg-[var(--t-warn-soft)] px-3 py-2.5">
            <div className="flex items-start gap-2">
              <svg className="h-3.5 w-3.5 text-[var(--t-warn)] mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
              </svg>
              <div>
                <div className="text-[14px] font-semibold text-[var(--t-warn)]">Model no longer available</div>
                <div className="text-[14px] text-[var(--t-warn)] mt-0.5 leading-snug">{groqHealthMessage(health)}</div>
              </div>
            </div>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {messages.length === 0 && (
            <div className="space-y-4 pt-2">
              <div className="text-center space-y-1.5">
                <div className="text-[14px] text-[var(--t-dim)] font-medium">Ask anything about your inventory</div>
                <div className="text-[14px] text-[var(--t-soft)]">
                  {folderCount > 0
                    ? `Knows your folders, forecasts, reorders and events`
                    : "Knows your 3-month forecasts, settings, events and POs"}
                </div>
              </div>
              <div className="grid grid-cols-1 gap-2">
                {/* Dynamic suggested prompts — show folder-aware ones if folders exist */}
                {[
                  ...(folderCount > 0 && folders
                    ? Object.values(folders).slice(0, 1).map(f =>
                        `Which SKUs in "${f.name}" need the most attention?`
                      )
                    : []),
                  ...SUGGESTED_PROMPTS,
                ].slice(0, 4).map((p, i) => (
                  <button key={i} onClick={() => sendMessage(p)}
                    className="text-left px-3 py-2.5 rounded-xl bg-[var(--t-line)] border border-[var(--t-line2)] hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group">
                    <span className="text-[14px] text-[var(--t-dim)] group-hover:text-[var(--t-accent)] transition-colors leading-snug">{p}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => <MessageBubble key={i} msg={msg} />)}
          {loading && <TypingIndicator />}
          {loading && waitNote && (
            <div className="text-[12.5px] text-[var(--t-soft)] pl-9">{waitNote}</div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Clear */}
        {messages.length > 0 && (
          <div className="px-4 pb-1 flex justify-end">
            <button onClick={() => { setMessages([]); setError(null); }}
              className="text-[13px] text-[var(--t-soft)] hover:text-[var(--t-dim)] transition-colors">
              Clear conversation
            </button>
          </div>
        )}

        {/* Input */}
        <div className="shrink-0 px-4 pb-5 pt-2">
          {error && (
            <div className="mb-2 text-[13px] text-[var(--t-bad)] bg-[var(--t-bad-soft)] border border-[var(--t-bad-line)] rounded-lg px-3 py-2">{error}</div>
          )}
          <div className="flex items-center justify-between mb-2">
            <button onClick={() => setIncludeHistory(v => !v)} role="switch" aria-checked={includeHistory}
              title="Gives the assistant each SKU's full monthly and recent daily sales so it can answer questions about past sales. It's large, so it needs a Groq tier above the free 12,000 tokens/minute."
              className="flex items-center gap-2 text-[13px] text-[var(--t-dim)] hover:text-[var(--t-soft)] transition-colors select-none">
              <span className={`relative h-4 w-7 rounded-full transition-colors shrink-0 ${includeHistory ? "bg-[var(--t-accent-soft)]" : "bg-[var(--t-line)]"}`}>
                <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-[var(--t-panel)] transition-all ${includeHistory ? "left-[14px]" : "left-0.5"}`} />
              </span>
              <span>Full sales history: <span className={includeHistory ? "text-[var(--t-accent)] font-semibold" : "font-semibold"}>{includeHistory ? "on" : "off"}</span></span>
            </button>
            {includeHistory && <span className="text-[12.5px] text-[var(--t-warn)]">needs higher Groq tier</span>}
          </div>
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKey}
              rows={1}
              placeholder="Ask about folders, reorders, forecasts, risks…"
              className="flex-1 bg-[var(--t-bg)] border border-[var(--t-line2)] focus:border-[var(--t-accent-line)] rounded-xl px-3 py-2.5 text-[14px] text-[var(--t-ink)] placeholder-[var(--t-line2)] focus:outline-none resize-none leading-relaxed transition-colors"
              style={{ minHeight: "38px", maxHeight: "120px", overflowY: "auto" }}
              onInput={e => {
                e.target.style.height = "auto";
                e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
              }}
            />
            <button
              onClick={() => sendMessage()}
              disabled={!input.trim() || loading}
              className="h-9 w-9 rounded-xl bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] disabled:bg-[var(--t-line)] disabled:cursor-not-allowed flex items-center justify-center transition-all shrink-0"
            >
              {loading ? (
                <div className="h-3 w-3 border-2 border-[var(--t-line2)] border-t-transparent rounded-full animate-spin" />
              ) : (
                <svg className="h-3.5 w-3.5 text-[var(--t-ink)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              )}
            </button>
          </div>
          <div className="mt-2 text-[13px] text-[var(--t-soft)] text-center">Enter to send · Shift+Enter for new line · Drag grip to reposition</div>
        </div>
      </div>
    </>
  );
}
