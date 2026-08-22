import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import AiDrawer from './AiDrawer';
import ScorecardTab from './ScorecardTab';
import BacktestTab from './BacktestTab';
import LiveAccuracy from './components/LiveAccuracy';
import { useArrivalPrompts, ArrivalPromptList, ArrivalPromptBanner } from './components/ArrivalPrompts';
import CategorizePanel from './CategorizePanel';
import SkuDetailPanel from './components/SkuDetailPanel';
import SupplierPanel from './components/SupplierPanel';
import UploadPanel from './components/UploadPanel';
import { FleetBento } from './components/FleetViews';
import { SkuListItem, FolderRow } from './components/Sidebar';
import { AssignFolderModal } from './components/modals';
import { ErrorToasts } from './components/common';
import { API, TZ, fetchJson } from './lib/api';
import { loadStorage, saveStorage } from './lib/storage';
import { GROQ_API_KEY } from './lib/ai';
import { DEFAULT_PARAMS, autoStrategy, makeFolderId, poEtaDays, planningLeadTime, leadTimeConfirmed, planRecompute, sanitizeParams, sanitizeAllParams, namedSuppliers, buildScorecardBody } from './lib/helpers';

// ─── ROOT APP ─────────────────────────────────
export default function App() {
  const [lightMode,    setLightMode]    = useState(false);
  const [skuList,      setSkuList]      = useState([]);
  const [activeSku,    setActiveSku]    = useState(null);
  const [activeView,   setActiveView]   = useState("fleet"); // "fleet" | "suppliers" | "scorecard"
  const [showProducts, setShowProducts] = useState(false);   // products drawer (slide-over over the content)
  const [palette,      setPalette]      = useState(false);   // ⌘K command palette
  const [paletteQ,     setPaletteQ]     = useState("");
  // Backtest results live here (not inside BacktestTab) so they survive tab switches
  // and a browser refresh.
  const [btRes,        setBtRes]        = useState(() => loadStorage("logitrack_backtest_result", null));
  const [btDiag,       setBtDiag]       = useState(() => loadStorage("logitrack_backtest_diagnosis", null));
  const [showCategorize, setShowCategorize] = useState(false);
  const [skuForecasts, setSkuForecasts] = useState([]);
  const [scorecardRows, setScorecardRows] = useState([]);
  const skuForecastsRef = useRef([]);
  useEffect(() => { skuForecastsRef.current = skuForecasts; }, [skuForecasts]);
  const [isUploading,  setIsUploading]  = useState(false);
  const [uploadError,  setUploadError]  = useState(null);
  const [uploadInfo,   setUploadInfo]   = useState(null);
  const [showUploadDetails, setShowUploadDetails] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(320);   // draggable sidebar width
  const [importHeight, setImportHeight] = useState(null);  // draggable Import section height (null = auto)
  // Repaired on the way in. A blank saved by an earlier build would otherwise replay on
  // every reload, so fixing the writer alone would never fix an affected browser.
  const [skuParams,    setSkuParams]    = useState(() => sanitizeAllParams(loadStorage("logitrack_params", {})));
  // Annual holding cost %. Shared, because it changes which protection level is cheapest
  // for EVERY product — it can't live inside one tab's local state.
  const [holdingPct,   setHoldingPct]   = useState(() => loadStorage("logitrack_holding", 25));
  const [openPOs,      setOpenPOs]      = useState(() => loadStorage("logitrack_pos",    {}));
  const [folders,      setFolders]      = useState(() => loadStorage("logitrack_folders", {}));
  const [suppliers,    setSuppliers]    = useState(() => loadStorage("logitrack_suppliers", {}));
  const [assignModal,  setAssignModal]  = useState(null);
  // Dismissals persist: an arrival you've said no to must not come back every hour.
  const [arrivalIgnored, setArrivalIgnored] = useState(() => loadStorage("logitrack_arrivals_ignored", {}));
  const [confirm,      setConfirm]      = useState(null);   // { title, body, confirmLabel, danger, onConfirm }
  const [pendingArrival, setPendingArrival] = useState(null);  // handed to the supplier tab

  useEffect(() => saveStorage("logitrack_backtest_result", btRes), [btRes]);
  useEffect(() => saveStorage("logitrack_backtest_diagnosis", btDiag), [btDiag]);

  /* Is a backtest in flight right now?
   *
   * `btRes` is restored from localStorage, so the fleet header happily reported a
   * headline accuracy figure from the PREVIOUS run seconds after an upload replaced the
   * data it was computed on. The Backtest tab already knew — it polls and warns — but it
   * kept that to itself, and the fleet header is the number people actually read.
   *
   * Status only, five seconds apart. The tab still owns fetching results; this exists so
   * the headline can refuse to state a number that's about to be superseded. */
  const [btBusy, setBtBusy] = useState(false);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const st = await (await fetch(`${API}/api/backtest/status`)).json();
        if (!stop) setBtBusy(st?.status === "running");
      } catch { if (!stop) setBtBusy(false); }   // backend down is not "running"
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  /* Live scoring, for the fleet headline.
   *
   * A backtest is the best evidence available on day one and the weakest by month six —
   * it replays a history the models were fitted near, and it stops being news. Real
   * graded weeks are strictly better evidence: those forecasts were sealed before the
   * week happened. So the headline uses the backtest only until live weeks exist, then
   * hands over. Cheap poll — the log is small and this is once a minute. */
  const [liveLog, setLiveLog] = useState(null);
  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const d = await (await fetch(`${API}/api/livelog`)).json();
        if (!stop) setLiveLog(d && Array.isArray(d.entries) ? d : null);
      } catch { /* leave whatever we had; a blip shouldn't blank the header */ }
    };
    pull();
    const id = setInterval(pull, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  // Persist suppliers
  useEffect(() => { saveStorage("logitrack_suppliers", suppliers); }, [suppliers]);

  const lm = lightMode; // shorthand
  // Paint <html>/<body> to match the theme so the overscroll "rubber-band" gutter
  // (the strip revealed when you scroll past an edge) shows the app background, not white.
  useEffect(() => {
    const bg = lightMode ? "#f1f5f9" : "#0c0c0e";
    document.documentElement.style.backgroundColor = bg;
    document.body.style.backgroundColor = bg;
  }, [lightMode]);

  useEffect(() => { saveStorage("logitrack_params",  skuParams); }, [skuParams]);
  useEffect(() => {
    saveStorage("logitrack_holding", holdingPct);
    // Push it server-side immediately: the upload's own automatic backtest reads this, so
    // setting your real rate before loading a file means the FIRST run already uses it.
    fetch(`${API}/api/settings/holding`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holding: Number(holdingPct) || 25 }),
    }).catch(() => {});
  }, [holdingPct]);
  useEffect(() => { saveStorage("logitrack_folders", folders);   }, [folders]);

  // ⌘K / Ctrl+K opens the command palette; Esc closes palette or drawer.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteQ("");
        setPalette(v => !v);
      } else if (e.key === "Escape") {
        setPalette(false);
        setShowProducts(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const createFolder       = () => { const id = makeFolderId(); setFolders(prev => ({ ...prev, [id]: { name: "New Folder", skuIds: [], collapsed: false, parentId: null } })); };
  const createSubfolder    = (parentId) => { const id = makeFolderId(); setFolders(prev => ({ ...prev, [parentId]: { ...prev[parentId], collapsed: false }, [id]: { name: "New Subfolder", skuIds: [], collapsed: false, parentId } })); };
  const renameFolder       = (folderId, name) => setFolders(prev => ({ ...prev, [folderId]: { ...prev[folderId], name } }));
  // Deleting a folder also removes any subfolders nested under it (their SKUs return to Ungrouped).
  const deleteFolder       = (folderId) => setFolders(prev => {
    const n = { ...prev }; delete n[folderId];
    Object.keys(n).forEach(id => { if (n[id]?.parentId === folderId) delete n[id]; });
    return n;
  });
  const toggleFolderCollapse = (folderId) => setFolders(prev => ({ ...prev, [folderId]: { ...prev[folderId], collapsed: !prev[folderId].collapsed } }));
  const assignSkuToFolder  = (folderId, skuId) => {
    setFolders(prev => {
      const cleaned = Object.fromEntries(Object.entries(prev).map(([id, f]) => [id, { ...f, skuIds: f.skuIds.filter(s => s !== skuId) }]));
      return { ...cleaned, [folderId]: { ...cleaned[folderId], skuIds: [...cleaned[folderId].skuIds, skuId] } };
    });
    setAssignModal(null);
  };
  const removeSkuFromFolder = (folderId, skuId) =>
    setFolders(prev => ({ ...prev, [folderId]: { ...prev[folderId], skuIds: prev[folderId].skuIds.filter(s => s !== skuId) } }));

  // Always merge over defaults so a partial entry (e.g. a SKU that only has a
  // unitCost/fees set from the Scorecard) still has stock/leadTime/coverage —
  // otherwise the forecast URL gets "undefined" and the API returns 422.
  const getParams = id => sanitizeParams({ ...DEFAULT_PARAMS, ...(skuParams[id] || {}) });

  const loadScorecardRows = useCallback(async (list = skuList, params = skuParams, pos = openPOs) => {
    if (!list.length) { setScorecardRows([]); return; }
    const res = await fetchJson(`${API}/api/scorecard`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Shared with ScorecardTab and AiDrawer — one question, one answer.
      body: JSON.stringify(buildScorecardBody(list, params, pos, suppliers)),
    }, "Loading scorecard");
    setScorecardRows(res?.rows || []);
  }, [skuList, skuParams, openPOs]);

  const refreshSkuSummary = useCallback(async (skuId, p) => {
    const { stock, leadTime, coverage } = p;
    const po = openPOs[skuId];
    const etaQ = poEtaDays(po) != null ? `&on_order_eta_days=${poEtaDays(po)}` : "";
    const plt1 = planningLeadTime(skuId, p, suppliers).days;
    const oq = po ? po.qty : 0;
    const existing = skuForecastsRef.current.find(f => f.skuId === skuId);
    const strategy = autoStrategy(existing?.demandVolatilityColor);
    // Pass cost/protection so the fleet view's protection level matches the detail
    // panel (otherwise it falls back to Standard with no margin known).
    const costQ = (p.unitCost != null && p.unitCost !== "") ? `&unit_cost=${p.unitCost}` : "";
    const feesQ = (p.fees != null && p.fees !== "") ? `&fees=${p.fees}` : "";
    const protQ = p.protection ? `&protection=${p.protection}` : "";
    // plt1 is the PLANNED lead time (lane-aware, clamped). `leadTime` is the raw field and
    // must never reach the API — that mismatch is what 422'd.
    const fc = await fetchJson(`${API}/api/forecast?sku_id=${encodeURIComponent(skuId)}&stock=${stock}&lead_time_days=${plt1}&coverage_days=${coverage}&strategy=${strategy}&forecast_months=3&units_on_order=${oq}${etaQ}${costQ}${feesQ}${protQ}&tz=${TZ}`,
      undefined, `Forecast for ${skuId}`);
    if (!fc) return;
    setSkuForecasts(prev => [...prev.filter(f => f.skuId !== skuId), fc]);
  }, [openPOs]);

  const updateParams = useCallback((id, p) => { setSkuParams(prev => ({ ...prev, [id]: p })); refreshSkuSummary(id, p); }, [refreshSkuSummary]);

  // Add received units to a SKU's on-hand stock, then refresh its forecast so the
  // dashboard (and scorecard) reflect the new level. Used when an order arrives.
  const receiveStock = useCallback((skuId, qty) => {
    const q = Number(qty);
    if (!skuId || !Number.isFinite(q) || q === 0) return;
    const cur  = { ...DEFAULT_PARAMS, ...(skuParams[skuId] || {}) };
    const next = { ...cur, stock: (Number(cur.stock) || 0) + q };
    setSkuParams(prev => { const u = { ...prev, [skuId]: next }; saveStorage("logitrack_params", u); return u; });
    refreshSkuSummary(skuId, next);
  }, [skuParams, refreshSkuSummary]);

  const loadSkuList = useCallback(async (currentParams, currentPOs) => {
    const params = currentParams ?? {};
    const pos    = currentPOs    ?? {};
    const data   = (await fetchJson(`${API}/api/skus`, undefined, "Loading products")) || [];
    setSkuList(data);
    const forecasts = await Promise.all(data.map(s => {
      const p  = { ...DEFAULT_PARAMS, ...(params[s.id] || {}) };
      const oq = pos[s.id] ? pos[s.id].qty : 0;
      const eta = poEtaDays(pos[s.id]);
      const etaQ2 = eta != null ? `&on_order_eta_days=${eta}` : "";
      const plt = planningLeadTime(s.id, p, suppliers).days;
      return fetchJson(`${API}/api/forecast?sku_id=${encodeURIComponent(s.id)}&stock=${p.stock}&lead_time_days=${plt}&coverage_days=${p.coverage}&strategy=balanced&forecast_months=3&units_on_order=${oq}${etaQ2}&tz=${TZ}`,
        undefined, `Forecast for ${s.id}`);
    }));
    setSkuForecasts(forecasts.filter(Boolean));
    await loadScorecardRows(data, params, pos);
    if (!activeSku && data.length === 1) setActiveSku(data[0].id);
  }, [activeSku, loadScorecardRows]);

  // Bumped to force the open SKU detail panel to refetch its forecast (e.g. after
  // categorization re-routes a product). Fleet-level data refreshes via loadSkuList.
  const [refreshNonce, setRefreshNonce] = useState(0);
  const onCatalogChanged = useCallback(async () => {
    await loadSkuList(skuParams, openPOs);
    await loadScorecardRows(skuList, skuParams, openPOs);
    setRefreshNonce(n => n + 1);
  }, [loadSkuList, loadScorecardRows, skuList, skuParams, openPOs]);

  useEffect(() => { loadSkuList(skuParams, openPOs); }, []);
  useEffect(() => { skuList.forEach(s => refreshSkuSummary(s.id, getParams(s.id))); }, [openPOs]);
  useEffect(() => { loadScorecardRows(skuList, skuParams, openPOs); }, [skuList, skuParams, openPOs, loadScorecardRows]);

  // ── The measured protection recommendation, kept current on its own ──────────
  // The backtest is what grounds the stockout protection in your actual history, and
  // nobody should have to go find a tab to get it. It starts itself on upload, re-starts
  // whenever an input that changes the answer changes, and reports progress so the
  // dashboard can say "calculating" instead of quietly showing the weaker cost curve.
  const [btJob, setBtJob] = useState({ status: "idle" });
  const btPoll = useRef(null);

  // Pairs we've already asked the backend to measure. Without this, a pair that CAN'T be
  // measured (every product on it too short to test) would be re-requested after every
  // run and loop forever.
  const btAsked = useRef(new Set());

  const watchBacktest = useCallback(() => {
    clearInterval(btPoll.current);
    setBtJob({ status: "running" });
    skuList.forEach((s) => refreshSkuSummary(s.id, getParams(s.id)));   // flip to "calculating" now
    const started = Date.now();
    btPoll.current = setInterval(async () => {
      const st = await fetchJson(`${API}/api/backtest/status`).catch(() => null);
      if (st) setBtJob(st);
      if (st && st.status === "running" && Date.now() - started < 20 * 60 * 1000) return;
      clearInterval(btPoll.current);
      // Pull fresh forecasts so "calculating…" resolves into the measured number.
      skuList.forEach((s) => refreshSkuSummary(s.id, getParams(s.id)));
      loadScorecardRows(skuList, skuParams, openPOs);
      // A finished run is the moment to check whether anything is STILL unmeasured. The
      // upload's own run only covers the defaults, so a product on a custom lead time
      // needs a follow-up — and this is the only point where we know one is due.
      ensurePairsMeasured();
    }, 4000);
  }, [skuList, skuParams, openPOs, refreshSkuSummary, loadScorecardRows]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Catch a run the backend started on its own (e.g. an upload) even across a reload —
  // and, crucially, cover pairs nothing ever asked for.
  //
  // Change-detection alone left a hole: a product whose lead time you set in an earlier
  // session never *changes*, so the debounced effect never fires, and the upload's own
  // run only covers the defaults. That product sat on "re-testing…" indefinitely while
  // its pair was never measured. So compare what the fleet NEEDS against what's actually
  // been measured, and fill the gap.
  const needSig = JSON.stringify(skuList.map((s) => {
    const q = getParams(s.id);
    return `${planningLeadTime(s.id, q, suppliers, { ignoreOneOff: true }).days}/${q.coverage ?? DEFAULT_PARAMS.coverage}`;
  }).sort());
  const ensurePairsMeasured = useCallback(async () => {
    if (!skuList.length) return;
    const st = await fetchJson(`${API}/api/backtest/status`).catch(() => null);
    if (!st) return;
    const need = new Set(JSON.parse(needSig));
    const have = new Set(st.cachedCombos || []);
    // Request anything unmeasured EVEN IF a run is in flight — the backend queues it and
    // starts it when the current one finishes. Returning early here was the bug: a page
    // load during the upload's own run skipped this check, and nothing ever came back to it.
    const missing = [...need].filter((k) => !have.has(k) && !btAsked.current.has(k));
    if (missing.length) {
      missing.forEach((k) => btAsked.current.add(k));
      triggerBacktest("missing-pairs");
    } else if (st.status === "running") {
      watchBacktest();
    } else {
      setBtJob(st);
    }
  }, [skuList, needSig, watchBacktest]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { ensurePairsMeasured(); }, [needSig]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Everything below changes the recommendation, so each one invalidates the last run:
  // lead time and coverage set the risk window, unit cost and fees set the economics.
  const btDebounce = useRef(null);
  // Previous per-product lead/coverage, so a change can be attributed to the products it
  // actually affects — changing one product's lead time shouldn't refit the other 17.
  const btPrevPairs = useRef(null);
  // Two kinds of change, two very different costs to answer them.
  // STRUCTURAL (lead time, coverage) changes which windows the buffer is judged over —
  // that needs a real re-run. ECONOMIC (cost, fees, holding) only changes what the
  // already-measured units are worth, so it re-prices the stored rows in milliseconds.
  /* Products whose lead time is still an untouched default are skipped, exactly as the
     engine already skips products with no unit cost — a backtest against a placeholder
     reads as evidence when it is a guess. They join automatically once you set one. */
  const btReady = useMemo(
    () => skuList.filter(s => leadTimeConfirmed(s.id, getParams(s.id), suppliers)).map(s => s.id),
    [skuList, skuParams, suppliers]);   // eslint-disable-line react-hooks/exhaustive-deps
  const btWaiting = useMemo(
    () => skuList.filter(s => !leadTimeConfirmed(s.id, getParams(s.id), suppliers)).map(s => s.id),
    [skuList, skuParams, suppliers]);   // eslint-disable-line react-hooks/exhaustive-deps

  const btSignature = JSON.stringify(
    Object.entries(skuParams).sort(([a], [b]) => a.localeCompare(b))
      .filter(([id]) => btReady.includes(id))
      .map(([id, p]) => [id, planningLeadTime(id, p, suppliers, { ignoreOneOff: true }).days, p?.coverage ?? DEFAULT_PARAMS.coverage])
  );
  const btCostSignature = JSON.stringify(
    Object.entries(skuParams).sort(([a], [b]) => a.localeCompare(b))
      .map(([id, p]) => [id, p?.unitCost ?? null, p?.fees ?? null]).concat([["holding", holdingPct]])
  );
  const btFirstRun = useRef(true);
  useEffect(() => {
    if (btFirstRun.current) { btFirstRun.current = false; return; }   // don't fire on mount
    if (!skuList.length) return;
    clearTimeout(btDebounce.current);
    // Debounced: each run refits every engine at every cutoff, so firing per keystroke
    // while someone types a lead time would queue minutes of work for nothing.
    btDebounce.current = setTimeout(() => {
      // Only products whose lead time is settled get a fingerprint. One that isn't ready
      // is simply absent; the moment it becomes ready it appears, and planRecompute reads
      // that as "needs testing" rather than silently skipping it forever.
      const now = {};
      skuList.filter((s) => btReady.includes(s.id)).forEach((s) => {
        const q = getParams(s.id);
        now[s.id] = `${planningLeadTime(s.id, q, suppliers, { ignoreOneOff: true }).days}/${q.coverage ?? DEFAULT_PARAMS.coverage}`;
      });
      const prev = btPrevPairs.current;
      btPrevPairs.current = now;
      const plan = planRecompute(prev, now);
      if (plan.kind === "none") return;
      if (plan.kind === "partial") triggerPartial(plan.skus);
      else triggerBacktest(plan.reason);
    }, 4000);
    return () => clearTimeout(btDebounce.current);
  }, [btSignature]);   // eslint-disable-line react-hooks/exhaustive-deps

  const costDebounce = useRef(null);
  const costFirstRun = useRef(true);
  useEffect(() => {
    if (costFirstRun.current) { costFirstRun.current = false; return; }
    if (!skuList.length) return;
    clearTimeout(costDebounce.current);
    costDebounce.current = setTimeout(async () => {
      const costs = {};
      Object.entries(skuParams).forEach(([sku, q]) => {
        const c = Number(q?.unitCost);
        if (Number.isFinite(c) && q?.unitCost !== "" && q?.unitCost != null) costs[sku] = { cost: c, fees: Number(q?.fees) || 0 };
      });
      try {
        const r = await fetch(`${API}/api/backtest/recost`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ costs, holding: Number(holdingPct) || 25 }),
        });
        const d = await r.json();
        // No stored run to re-price (first session, or a restart) → do it the slow way.
        if (!d?.recosted) { triggerBacktest("costs-changed"); return; }
        skuList.forEach((s) => refreshSkuSummary(s.id, getParams(s.id)));
        loadScorecardRows(skuList, skuParams, openPOs);
      } catch { /* keep what's on screen */ }
    }, 1500);
    return () => clearTimeout(costDebounce.current);
  }, [btCostSignature]);   // eslint-disable-line react-hooks/exhaustive-deps

  const triggerPartial = useCallback(async (skus) => {
    const costs = {};
    Object.entries(skuParams).forEach(([sku, q]) => {
      const c = Number(q?.unitCost);
      if (Number.isFinite(c) && q?.unitCost !== "" && q?.unitCost != null) costs[sku] = { cost: c, fees: Number(q?.fees) || 0 };
    });
    const seen = new Set(), combos = [];
    skuList.filter(s => btReady.includes(s.id)).forEach((s) => {
      const q = getParams(s.id);
      const L = planningLeadTime(s.id, q, suppliers, { ignoreOneOff: true }).days, C = Number(q.coverage ?? DEFAULT_PARAMS.coverage);
      const k = `${L}/${C}`;
      if (!seen.has(k)) { seen.add(k); combos.push({ lead: L, coverage: C }); }
    });
    skus = skus.filter(id => btReady.includes(id));
    if (!skus.length) return;
    try {
      const r = await fetch(`${API}/api/backtest/partial`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skus, combos, costs, holding: Number(holdingPct) || 25 }),
      });
      const d = await r.json();
      if (!d?.started) { triggerBacktest("params-changed"); return; }   // nothing to merge into
      watchBacktest();
    } catch { triggerBacktest("params-changed"); }
  }, [skuList, skuParams, holdingPct, watchBacktest]);   // eslint-disable-line react-hooks/exhaustive-deps

  const triggerBacktest = useCallback(async (trigger) => {
    // Send EVERY distinct lead/coverage pair your products use, not just the commonest.
    // Scoring extra pairs is nearly free (the models are fitted once and scored at each),
    // and without this a product whose lead time you changed could never be re-measured —
    // the run used the fleet's typical values, so its own pair stayed untested forever.
    const seen = new Set(), combos = [];
    skuList.filter(s => btReady.includes(s.id)).forEach((s) => {
      const q = getParams(s.id);
      const L = planningLeadTime(s.id, q, suppliers, { ignoreOneOff: true }).days;
      const C = Number(q.coverage ?? DEFAULT_PARAMS.coverage);
      const k = `${L}/${C}`;
      if (!seen.has(k)) { seen.add(k); combos.push({ lead: L, coverage: C }); }
    });
    if (!combos.length) return;   // nothing has a confirmed lead time yet
    const commonest = (key, fallback) => {
      const counts = {};
      skuList.forEach((s) => { const v = getParams(s.id)[key] ?? fallback; counts[v] = (counts[v] || 0) + 1; });
      const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
      return best ? Number(best[0]) : fallback;
    };
    const costs = {};
    Object.entries(skuParams).forEach(([sku, p]) => {
      const c = Number(p?.unitCost);
      if (Number.isFinite(c) && p?.unitCost !== "" && p?.unitCost != null) costs[sku] = { cost: c, fees: Number(p?.fees) || 0 };
    });
    try {
      await fetch(`${API}/api/backtest/refresh`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead: commonest("leadTime", DEFAULT_PARAMS.leadTime),
                               coverage: commonest("coverage", DEFAULT_PARAMS.coverage),
                               combos, costs, holding: Number(holdingPct) || 25, trigger }),
      });
      watchBacktest();
    } catch { /* a failed trigger just leaves the cost-curve estimate in place */ }
  }, [skuList, skuParams, holdingPct, watchBacktest]);   // eslint-disable-line react-hooks/exhaustive-deps

  const handleUploadSuccess = async (info) => {
    setUploadInfo(info); setUploadError(null);
    const updatedParams = { ...skuParams };
    (info.loadedSkus || []).forEach(sku => {
      if (sku.lastKnownStock !== null && sku.lastKnownStock !== undefined) {
        const existing = updatedParams[sku.id] ?? DEFAULT_PARAMS;
        updatedParams[sku.id] = { ...existing, stock: sku.lastKnownStock };
      }
      // A Cost/Unit_Cost column in the file seeds the unit-cost field so margin /
      // protection economics work out of the box — but NEVER overwrite a cost the
      // user typed in by hand (the sheet may be staler than their entry).
      if (sku.lastKnownCost !== null && sku.lastKnownCost !== undefined) {
        const existing = updatedParams[sku.id] ?? DEFAULT_PARAMS;
        if (existing.unitCost === undefined || existing.unitCost === null || existing.unitCost === "") {
          updatedParams[sku.id] = { ...existing, unitCost: sku.lastKnownCost };
        }
      }
    });
    setSkuParams(updatedParams); saveStorage("logitrack_params", updatedParams);
    await loadSkuList(updatedParams, openPOs);
    if (info.loadedSkus?.length === 1) setActiveSku(info.loadedSkus[0].id);
    // The backend already kicked off a backtest on ingest using costs from the sheet.
    // Watch it so the dashboard shows "calculating" and then swaps in the measured
    // recommendation. The debounced effect will re-run it later if the user edits costs.
    btFirstRun.current = true;    // seeding params from the sheet isn't a user edit
    btAsked.current = new Set();  // a new sheet invalidates what was measured before
    if (info.backtestStarted) watchBacktest();
  };

  const doDeleteSku = async (skuId) => {
    await fetchJson(`${API}/api/skus/${encodeURIComponent(skuId)}`, { method: "DELETE" }, "Deleting product");
    if (activeSku === skuId) setActiveSku(null);
    setSkuParams(prev => { const n = { ...prev }; delete n[skuId]; return n; });
    const nextPOs = { ...openPOs }; delete nextPOs[skuId];
    setOpenPOs(nextPOs); saveStorage("logitrack_pos", nextPOs);
    setFolders(prev => Object.fromEntries(Object.entries(prev).map(([id, f]) => [id, { ...f, skuIds: f.skuIds.filter(s => s !== skuId) }])));
    await loadSkuList(skuParams, openPOs);
  };
  // Always confirm before a destructive delete.
  const handleDeleteSku = (skuId) => {
    const name = skuList.find(s => s.id === skuId)?.name || skuId;
    setConfirm({
      title: "Delete this product?",
      body: `"${name}" will be removed along with its forecast, parameters, and any folder assignment. This can't be undone.`,
      confirmLabel: "Delete", danger: true,
      onConfirm: () => doDeleteSku(skuId),
    });
  };

  // Auto-organize products into nested folders that mirror the Grouping tab:
  // one folder per AI category, with a subfolder per AI subcategory beneath it.
  const autoFolderByCategory = async () => {
    const data = await fetchJson(`${API}/api/groups`, undefined, "Loading product groups");
    const deadIds = new Set((scorecardRows || []).filter(r => r.status === "Dead stock").map(r => r.skuId));
    // Fallback attribute keys that name a sub-family, used only when the engine didn't
    // form a formal subgroup (below). The engine's own adaptive split is the primary signal.
    const FAMILY_KEYS = ["subcategory", "sub_category", "vanity_type", "product_type", "type", "family", "product_family", "collection", "class"];
    // Clean a raw attribute / group fragment into a folder title: strip any "key:" prefix
    // ("subcategory:Vanity Cabinet Only" → "Vanity Cabinet Only"), de-underscore, collapse
    // whitespace, and title-case all-lowercase values.
    const cleanTitle = (v) => {
      if (v == null) return "";
      let s = String(v).trim();
      const m = s.match(/^[a-z][a-z _-]*:\s*(.+)$/i);
      if (m) s = m[1].trim();
      s = s.replace(/_/g, " ").replace(/\s+/g, " ").trim();
      if (s && !/[A-Z]/.test(s)) s = s.replace(/\b\w/g, c => c.toUpperCase());
      return s;
    };
    // An "assorted / mixed / warehouse lot / bundle" product is deliberately heterogeneous,
    // so pinning it to a precise subcategory is misleading — it stays directly under its
    // category (or ungrouped) instead. Matches the name or category text.
    const isAssorted = (s, cat) => /\b(assorted|mixed|miscellaneous|misc|various|bundle|sampler|grab\s?bag|job\s?lot|pallet|warehouse|lot)\b/i.test(`${s.skuName || ""} ${cat || ""} ${s.attributes?.category || ""}`);
    // Derive a SKU's subcategory the SAME way the engine/Grouping tab does, so folders
    // match what you see there. Priority:
    //   1) the engine's adaptive subgroup — s.group = "Base | col:val | col2:val2". The
    //      engine only forms this when the split genuinely improves pooling, so trust ALL
    //      of its split columns (bag size, grind type, roast, …) and join their values.
    //   2) a shared family attribute, for catalogs where no formal subgroup formed.
    const deriveSub = (s, attrs, cat) => {
      if (isAssorted(s, cat)) return "";
      const g = s.group != null ? String(s.group) : "";
      if (g.includes("|")) {
        const label = g.split("|").slice(1)
          .map(p => { const i = p.indexOf(":"); return cleanTitle(i >= 0 ? p.slice(i + 1) : p); })
          .filter(Boolean).join(" · ");
        if (label && label.toLowerCase() !== cat.toLowerCase()) return label;
      }
      for (const k of FAMILY_KEYS) { if (attrs[k]) { const v = cleanTitle(attrs[k]); if (v && v.toLowerCase() !== cat.toLowerCase()) return v; } }
      return "";
    };
    // category → { direct: [skuIds without a subcategory], subs: { subName → [skuIds] } }.
    // Category is the engine's base group (what the Grouping tab shows as the category).
    const tree = {};
    (data?.skus || []).forEach(s => {
      if (deadIds.has(s.skuId)) return;
      const attrs = s.attributes || {};
      const cat = cleanTitle(s.baseGroup) || cleanTitle(attrs.category);
      if (!cat) return;
      const sub = deriveSub(s, attrs, cat);
      const node = (tree[cat] ||= { direct: [], subs: {} });
      if (sub) (node.subs[sub] ||= []).push(s.skuId);
      else node.direct.push(s.skuId);
    });
    const cats = Object.keys(tree);
    if (!cats.length) {
      setConfirm({ title: "No categories to fold", body: "Your products don't have categories assigned yet. Open the Grouping tab so they get classified, then try again.", confirmLabel: "Got it", onConfirm: () => {} });
      return;
    }
    const subCount = cats.reduce((n, c) => n + Object.keys(tree[c].subs).length, 0);
    setConfirm({
      title: "Auto-organize into folders?",
      body: `This creates ${cats.length} category folder${cats.length !== 1 ? "s" : ""}${subCount ? ` with ${subCount} subfolder${subCount !== 1 ? "s" : ""}` : ""} — mirroring the Grouping tab (${cats.slice(0, 4).join(", ")}${cats.length > 4 ? "…" : ""}) — and sorts your products in. You can rename, move, or remove anything afterward.`,
      confirmLabel: "Create folders",
      onConfirm: () => setFolders(prev => {
        const next = { ...prev };
        const findTop = (name) => Object.entries(next).find(([, f]) => !f.parentId && (f.name || "").toLowerCase() === name.toLowerCase());
        const findSub = (parentId, name) => Object.entries(next).find(([, f]) => f.parentId === parentId && (f.name || "").toLowerCase() === name.toLowerCase());
        const placements = [];               // { fid, ids } — where each SKU should land
        const placedIds = new Set();
        const wasNonEmpty = {};              // folders that held SKUs before this reorg
        Object.entries(next).forEach(([id, f]) => { wasNonEmpty[id] = (f.skuIds || []).length > 0; });
        cats.forEach(cat => {
          const ex = findTop(cat);
          const fid = ex ? ex[0] : makeFolderId();
          next[fid] = { collapsed: false, ...(next[fid] || {}), name: cat, parentId: null, skuIds: next[fid]?.skuIds || [] };
          if (tree[cat].direct.length) { placements.push({ fid, ids: tree[cat].direct }); tree[cat].direct.forEach(id => placedIds.add(id)); }
          Object.entries(tree[cat].subs).forEach(([subName, ids]) => {
            const exs = findSub(fid, subName);
            const sfid = exs ? exs[0] : makeFolderId();
            next[sfid] = { collapsed: false, ...(next[sfid] || {}), name: subName, parentId: fid, skuIds: next[sfid]?.skuIds || [] };
            placements.push({ fid: sfid, ids }); ids.forEach(id => placedIds.add(id));
          });
        });
        // Remove the SKUs we're about to place from every folder first, so each lands in
        // exactly one place (its subfolder if it has a subcategory, else its category).
        Object.keys(next).forEach(id => { next[id] = { ...next[id], skuIds: (next[id].skuIds || []).filter(x => !placedIds.has(x)) }; });
        placements.forEach(({ fid, ids }) => { next[fid] = { ...next[fid], skuIds: [...new Set([...(next[fid].skuIds || []), ...ids])] }; });
        // Prune folders this reorg emptied (e.g. old composite-named folders whose SKUs
        // moved into clean category/subfolders) — but keep folders that were already
        // empty on purpose, current targets, and any parent that still has subfolders.
        const targetIds = new Set(placements.map(p => p.fid));
        Object.keys(next).forEach(id => {
          const hasChildren = Object.values(next).some(f => f.parentId === id);
          if (wasNonEmpty[id] && !targetIds.has(id) && (next[id].skuIds || []).length === 0 && !hasChildren) delete next[id];
        });
        return next;
      }),
    });
  };

  const handleReset = async () => {
    await fetchJson(`${API}/api/reset`, { method: "POST" }, "Reset");
    setUploadInfo(null); setUploadError(null); setActiveSku(null);
    setSkuParams({}); setOpenPOs({});
    saveStorage("logitrack_params", {}); saveStorage("logitrack_pos", {});
    await loadSkuList({}, {});
  };

  // ── Excel exports ──
  const [exporting, setExporting] = useState(null);   // which export is running
  const buildExportSkus = () => {
    const out = {};
    (skuList || []).forEach(s => {
      const p = skuParams[s.id] || {};
      const po = openPOs[s.id];
      out[s.id] = {
        stock: p.stock, leadTime: p.leadTime, coverage: p.coverage, months: p.months,
        unitCost: p.unitCost, fees: p.fees, protection: p.protection,
        unitsOnOrder: po ? po.qty : 0, hasOpenPo: !!po,
        // so the scorecard/export price a shipment by when it lands, like the panel does
        onOrderEtaDays: poEtaDays(po),
      };
    });
    return out;
  };
  const downloadExport = async (key, path, body, filename) => {
    setExporting(key);
    try {
      const res = await fetch(`${API}${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert("Could not generate the export — is the backend running? " + (e?.message || ""));
    } finally {
      setExporting(null);
    }
  };
  const _today = () => new Date().toISOString().slice(0, 10);   // YYYY-MM-DD
  const exportSku = (sid) => downloadExport("sku", `/api/export/sku/${encodeURIComponent(sid)}`, { skus: buildExportSkus() }, `${sid}_forecast_${_today()}.xlsx`);
  const exportFleet = () => downloadExport("fleet", "/api/export/fleet", { skus: buildExportSkus(), folders }, `logitrack_fleet_${_today()}.xlsx`);
  const exportSuppliers = () => downloadExport("suppliers", "/api/export/suppliers", { suppliers }, `logitrack_suppliers_${_today()}.xlsx`);
  const exportAll = () => downloadExport("all", "/api/export/all", { skus: buildExportSkus(), folders, suppliers }, `logitrack_full_report_${_today()}.xlsx`);

  const hasUploaded    = skuList.some(s => s.mode === "uploaded");
  const ungroupedSkus  = skuList.filter(s => !Object.values(folders).some(f => f.skuIds.includes(s.id)));
  const scoreBySkuApp  = Object.fromEntries((scorecardRows || []).map(r => [r.skuId, r]));

  // Theme tokens for shell
  const shellBg   = lm ? "bg-slate-100 text-slate-900" : "bg-[#0c0c0e] text-slate-100";
  const sidebarBg = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const headerBg  = lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10";
  const logoText  = lm ? "text-slate-900" : "text-white";
  const subText   = lm ? "text-slate-500" : "text-slate-500";
  const skusLabel = lm ? "text-slate-400" : "text-slate-600";
  const divider   = lm ? "border-slate-200" : "border-white/10";
  const fleetBtnActive   = lm ? "bg-slate-100 border-slate-300 text-slate-900" : "bg-slate-800/70 border-slate-700/60 text-white";
  const fleetBtnInactive = lm ? "border-transparent text-slate-500 hover:bg-slate-100 hover:text-slate-800" : "border-transparent text-slate-400 hover:bg-slate-800/40 hover:text-slate-200";
  const skuCountBadge    = lm ? "bg-slate-200 text-slate-500" : "bg-slate-700/60 text-slate-400";
  const sectionTitleText = lm ? "text-slate-900" : "text-white";
  const breadcrumbMuted  = lm ? "text-slate-500" : "text-slate-500";
  const breadcrumbSep    = lm ? "text-slate-300" : "text-slate-700";
  const skuTabActive     = lm ? "bg-slate-200 text-slate-900" : "bg-slate-700 text-white";
  const skuTabInactive   = lm ? "text-slate-500 hover:text-slate-800 hover:bg-slate-100" : "text-slate-500 hover:text-slate-300 hover:bg-slate-800/60";
  const dateText         = lm ? "text-slate-400" : "text-slate-600";
  const importLabel      = lm ? "text-slate-400" : "text-slate-600";
  const resetBtn         = lm ? "text-slate-500 hover:text-slate-700 border-slate-300 hover:border-slate-400 rounded-lg py-1.5" : "text-slate-500 hover:text-slate-300 border-white/10 hover:border-slate-700 rounded-lg py-1.5";
  const toggleBtn        = lm
    ? "h-7 w-7 rounded-lg border border-slate-300 bg-white flex items-center justify-center text-slate-600 hover:bg-slate-100 transition-all shrink-0"
    : "h-7 w-7 rounded-lg border border-slate-700 bg-slate-800/60 flex items-center justify-center text-slate-300 hover:bg-slate-700 transition-all shrink-0";

  const ThemeToggle = () => (
    <button onClick={() => setLightMode(v => !v)} title={lm ? "Switch to dark mode" : "Switch to light mode"} className={toggleBtn}>
      <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        {lm
          ? <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
          : <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M12 8a4 4 0 100 8 4 4 0 000-8z" />
        }
      </svg>
    </button>
  );

  /* Detected arrivals. Suggestions only — confirming is what writes a received date, and
     that stays a human decision because a wrong one corrupts the lead-time record. */
  const { found: arrivals, refresh: refreshArrivals } =
    useArrivalPrompts(API, suppliers, openPOs, arrivalIgnored);

  const ignoreArrival = useCallback((a) => {
    setArrivalIgnored(prev => {
      const next = { ...prev, [`${a.orderId}|${a.arrivedOn}`]: true };
      saveStorage("logitrack_arrivals_ignored", next);
      return next;
    });
  }, []);

  // "Yes" routes to the supplier card holding that order, where the arrival is recorded
  // with the date the stock actually moved rather than today's.
  const confirmArrival = useCallback((a) => {
    setActiveSku(null);
    setActiveView("suppliers");
    setPendingArrival(a);
  }, []);

  // ── drag-to-resize handlers (sidebar width + Import-section height) ──
  const startResizeWidth = (e) => {
    e.preventDefault();
    const move = (ev) => setSidebarWidth(Math.min(640, Math.max(240, ev.clientX)));
    const up = () => {
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
      document.body.style.cursor = ""; document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  };
  const startResizeImport = (e) => {
    e.preventDefault();
    const move = (ev) => setImportHeight(Math.min(window.innerHeight - 240, Math.max(110, window.innerHeight - ev.clientY)));
    const up = () => {
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
      document.body.style.cursor = ""; document.body.style.userSelect = "";
    };
    document.body.style.cursor = "row-resize"; document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  };

  return (
    <div className={`relative flex h-screen w-full ${shellBg} font-sans overflow-hidden`}>
      <style>{`.sku-scroll::-webkit-scrollbar{width:10px}.sku-scroll::-webkit-scrollbar-thumb{background:rgba(100,116,139,.55);border-radius:5px;border:2px solid transparent;background-clip:content-box}.sku-scroll::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.85);background-clip:content-box}.sku-scroll{scrollbar-width:thin;scrollbar-color:rgba(100,116,139,.55) transparent}
      .sku-strip::-webkit-scrollbar{height:5px}.sku-strip::-webkit-scrollbar-track{background:transparent}.sku-strip::-webkit-scrollbar-thumb{background:rgba(100,116,139,.4);border-radius:3px}.sku-strip::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.7)}.sku-strip{scrollbar-width:thin;scrollbar-color:rgba(100,116,139,.4) transparent}`}</style>

      {/* Products drawer (slide-over over the content) */}
      {showProducts && <div className="absolute inset-0 z-30 bg-black/50 backdrop-blur-sm" onClick={() => setShowProducts(false)} />}
      {showProducts && (
      <div style={{ width: sidebarWidth }} className={`absolute inset-y-0 left-0 z-40 shadow-2xl ${sidebarBg} border-r flex flex-col overflow-hidden`}>
        <div className={`px-5 pt-6 pb-5 border-b ${divider} flex items-center gap-3`}>
          <div className="h-8 w-8 bg-violet-600 rounded-xl flex items-center justify-center shadow-lg shadow-violet-900/30">
            <svg className="h-4 w-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
          </div>
          <div>
            <div className={`text-sm font-bold ${logoText} tracking-tight`}>LogiTrack</div>
            <div className={`text-[11px] ${subText} font-mono`}>Demand Intelligence</div>
          </div>
        </div>

        <div className="px-3 pt-4 pb-2 space-y-1">
          <button onClick={() => { setActiveSku(null); setActiveView("fleet"); }}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${activeSku === null && activeView === "fleet" ? fleetBtnActive : fleetBtnInactive}`}>
            <svg className="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
            </svg>
            <span className="text-sm font-semibold">Fleet Overview</span>
            <span className={`ml-auto text-[11px] font-mono ${skuCountBadge} px-1.5 py-0.5 rounded-md`}>{skuList.length}</span>
          </button>
          <button onClick={() => { setActiveSku(null); setActiveView("suppliers"); }}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${activeSku === null && activeView === "suppliers" ? fleetBtnActive : fleetBtnInactive}`}>
            <svg className="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
            </svg>
            <span className="text-sm font-semibold">Suppliers</span>
            <span className={`ml-auto text-[11px] font-mono ${skuCountBadge} px-1.5 py-0.5 rounded-md`}>{namedSuppliers(suppliers).length}</span>
          </button>
          <button onClick={() => { setActiveSku(null); setActiveView("scorecard"); }}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${activeSku === null && activeView === "scorecard" ? fleetBtnActive : fleetBtnInactive}`}>
            <svg className="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
            <span className="text-sm font-semibold">SKU Scorecard</span>
            <span className={`ml-auto text-[11px] font-mono ${skuCountBadge} px-1.5 py-0.5 rounded-md`}>{skuList.length}</span>
          </button>
          <button onClick={() => { setActiveSku(null); setActiveView("backtest"); }}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${activeSku === null && activeView === "backtest" ? fleetBtnActive : fleetBtnInactive}`}>
            <svg className="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <span className="text-sm font-semibold">Backtest</span>
          </button>
          <button onClick={() => { setActiveSku(null); setActiveView("live"); }}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${activeSku === null && activeView === "live" ? fleetBtnActive : fleetBtnInactive}`}>
            <svg className="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <span className="text-sm font-semibold">Live accuracy</span>
          </button>
        </div>

        <div className="px-3 flex-1 overflow-y-scroll sku-scroll space-y-0.5 pb-3">
          <div className="flex items-center justify-between px-1 py-2">
            <div className={`text-[10px] uppercase tracking-widest ${skusLabel} font-bold`}>SKUs</div>
            <div className="flex items-center gap-1.5">
              <button onClick={() => { setActiveSku(null); setActiveView("categorize"); setShowProducts(false); }} title="Categorize & group products"
                className={`h-9 w-9 rounded-lg flex items-center justify-center border transition-all ${lm ? "border-violet-200 bg-violet-50 text-violet-600 hover:bg-violet-100 hover:border-violet-300" : "border-violet-900/40 bg-violet-950/30 text-violet-300 hover:bg-violet-900/40 hover:border-violet-700"}`}>
                <svg className="h-[18px] w-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
                </svg>
              </button>
              <button onClick={autoFolderByCategory} title="Auto-organize products into folders by category"
                className={`h-9 w-9 rounded-lg flex items-center justify-center border transition-all ${lm ? "border-slate-200 bg-white text-slate-600 hover:bg-slate-100 hover:border-slate-300" : "border-slate-700/60 bg-slate-800/40 text-slate-300 hover:bg-slate-700/60 hover:border-slate-600"}`}>
                <svg className="h-[18px] w-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.75 9.776c.112-.017.227-.026.344-.026h15.812c.117 0 .232.009.344.026m-16.5 0a2.25 2.25 0 00-1.883 2.542l.857 6a2.25 2.25 0 002.227 1.932H19.05a2.25 2.25 0 002.227-1.932l.857-6a2.25 2.25 0 00-1.883-2.542m-16.5 0V6A2.25 2.25 0 016 3.75h3.879a1.5 1.5 0 011.06.44l2.122 2.12a1.5 1.5 0 001.06.44H18A2.25 2.25 0 0120.25 9v.776" />
                </svg>
              </button>
              <button onClick={createFolder} title="Add new folder"
                className={`h-9 w-9 rounded-lg flex items-center justify-center border transition-all ${lm ? "border-slate-200 bg-white text-slate-600 hover:bg-slate-100 hover:border-slate-300" : "border-slate-700/60 bg-slate-800/40 text-slate-300 hover:bg-slate-700/60 hover:border-slate-600"}`}>
                <svg className="h-[18px] w-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 13h6m-3-3v6m-9 1V7a2 2 0 012-2h6l2 2h6a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
                </svg>
              </button>
            </div>
          </div>

          {Object.entries(folders).filter(([, f]) => !f.parentId).map(([folderId, folder]) => (
            <FolderRow key={folderId} folderId={folderId} folder={folder} allFolders={folders} skuList={skuList} skuForecasts={skuForecasts}
              openPOs={openPOs} activeSku={activeSku} onSelectSku={setActiveSku} onDeleteSku={handleDeleteSku}
              onRename={renameFolder} onDelete={deleteFolder} onToggleCollapse={toggleFolderCollapse}
              onRemoveSkuFromFolder={removeSkuFromFolder} onAddSubfolder={createSubfolder} lm={lm} scoreBySku={scoreBySkuApp} />
          ))}

          {ungroupedSkus.map(sku => {
            const fc = skuForecasts.find(f => f.skuId === sku.id);
            return (
              <SkuListItem key={sku.id} sku={sku} isActive={activeSku === sku.id} onClick={() => setActiveSku(sku.id)}
                onDelete={handleDeleteSku} reorderDays={fc?.daysUntilReorder} hasOpenPO={!!openPOs[sku.id]} lm={lm}
                healthStatus={scoreBySkuApp[sku.id]?.status}
                provisional={fc?.tooNew || fc?.young}
                secondaryAction={Object.keys(folders).length > 0
                  ? { title: "Move to folder", onClick: () => setAssignModal({ skuId: sku.id, skuName: sku.name }), iconPath: "M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" }
                  : undefined} />
            );
          })}
        </div>

        <div onMouseDown={startResizeImport} title="Drag to resize"
          className={`h-1.5 shrink-0 cursor-row-resize ${lm ? "hover:bg-violet-300" : "hover:bg-violet-600/50"}`} />
        <div style={{ height: importHeight ?? undefined }} className={`p-4 border-t ${divider} space-y-3 shrink-0 overflow-y-auto sku-scroll`}>
          <div className={`text-[10px] uppercase tracking-widest ${importLabel} font-bold`}>Import Data</div>
          <UploadPanel onUploadSuccess={handleUploadSuccess} onUploadError={setUploadError}
            isUploading={isUploading} setIsUploading={setIsUploading} lm={lm} holdingPct={holdingPct} setHoldingPct={setHoldingPct} />
          {uploadError && (
            <div className={`text-[11px] rounded-lg p-2 leading-relaxed border ${lm ? "text-red-700 bg-red-50 border-red-200" : "text-rose-400 bg-rose-950/20 border-rose-900/30"}`}>{uploadError}</div>
          )}
          {/* Large date re-anchoring shifts move sales into different calendar months and
              can distort learned seasonality — the user must know this happened. */}
          {uploadInfo && Math.abs(uploadInfo.dateShiftDays || 0) > 21 && (
            <div className={`text-[11px] rounded-lg p-2 leading-relaxed border ${lm ? "text-amber-700 bg-amber-50 border-amber-300" : "text-amber-300 bg-amber-950/20 border-amber-900/40"}`}>
              ⚠️ <span className="font-semibold">Dates shifted {Math.abs(uploadInfo.dateShiftDays).toLocaleString()} days.</span>{" "}
              This file's data ends well in the past, so every date was moved forward to line up with today.
              A shift this large relocates sales into different months and can distort seasonal patterns —
              for accurate seasonality, re-export a file that runs up to the present.
            </div>
          )}
          {uploadInfo && (
            <div className={`text-[11px] rounded-lg border overflow-hidden ${lm ? "text-emerald-700 bg-emerald-50 border-emerald-200" : "text-emerald-400 bg-emerald-950/20 border-emerald-900/30"}`}>
              <button onClick={() => setShowUploadDetails(v => !v)} className="w-full flex items-center justify-between gap-2 p-2 text-left">
                <span className="truncate font-semibold">✓ Loaded {uploadInfo.loadedSkus?.length} SKU{uploadInfo.loadedSkus?.length !== 1 ? "s" : ""} from {uploadInfo.filename}</span>
                <svg className={`h-3.5 w-3.5 shrink-0 transition-transform ${showUploadDetails ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
              </button>
              {showUploadDetails && (
                <div className={`px-2 pb-2 pt-1.5 space-y-1 border-t leading-relaxed ${lm ? "border-emerald-200" : "border-emerald-900/40"}`}>
                  {uploadInfo.loadedSkus?.some(s => s.lastKnownStock != null) && (
                    <span className={`block ${lm ? "text-violet-700" : "text-violet-400"}`}>
                      Stock auto-detected: {uploadInfo.loadedSkus.filter(s => s.lastKnownStock != null).map(s => `${s.id}: ${s.lastKnownStock.toLocaleString()} units`).join(" · ")}
                    </span>
                  )}
                  {uploadInfo.loadedSkus?.some(s => s.lastKnownCost != null) && (
                    <span className={`block ${lm ? "text-violet-700" : "text-violet-400"}`}>
                      Unit costs auto-detected for {uploadInfo.loadedSkus.filter(s => s.lastKnownCost != null).length} SKU{uploadInfo.loadedSkus.filter(s => s.lastKnownCost != null).length !== 1 ? "s" : ""} — margins &amp; protection tiers will use them (your own entries are never overwritten)
                    </span>
                  )}
                  {uploadInfo.loadedSkus?.some(s => s.stockoutRowsDropped > 0) && (
                    <span className={`block ${lm ? "text-amber-600" : "text-amber-400"}`}>
                      {uploadInfo.loadedSkus.filter(s => s.stockoutRowsDropped > 0).map(s => `${s.id}: ${s.stockoutRowsDropped} zero-stock days excluded`).join(" · ")}
                    </span>
                  )}
                  {uploadInfo.dataQuality && (uploadInfo.dataQuality.duplicateRowsMerged > 0 || uploadInfo.dataQuality.missingSalesFilledZero > 0 || uploadInfo.dataQuality.badDatesDropped > 0 || uploadInfo.dataQuality.missingDaysFilled > 0) && (
                    <span className={`block ${lm ? "text-slate-500" : "text-slate-400"}`}>
                      Cleaned: {[
                        uploadInfo.dataQuality.duplicateRowsMerged > 0 && `${uploadInfo.dataQuality.duplicateRowsMerged} duplicate row${uploadInfo.dataQuality.duplicateRowsMerged !== 1 ? "s" : ""} merged (no double-counting)`,
                        uploadInfo.dataQuality.missingSalesFilledZero > 0 && `${uploadInfo.dataQuality.missingSalesFilledZero} missing sales value${uploadInfo.dataQuality.missingSalesFilledZero !== 1 ? "s" : ""} set to 0`,
                        uploadInfo.dataQuality.badDatesDropped > 0 && `${uploadInfo.dataQuality.badDatesDropped} unreadable date${uploadInfo.dataQuality.badDatesDropped !== 1 ? "s" : ""} dropped`,
                        uploadInfo.dataQuality.missingDaysFilled > 0 && `${uploadInfo.dataQuality.missingDaysFilled.toLocaleString()} missing calendar day${uploadInfo.dataQuality.missingDaysFilled !== 1 ? "s" : ""} filled as 0-sale days`,
                      ].filter(Boolean).join(" · ")} · {uploadInfo.dataQuality.rowsUsed?.toLocaleString()} rows used
                    </span>
                  )}
                  {uploadInfo.errors?.length > 0 && <span className={`block ${lm ? "text-amber-600" : "text-amber-400"}`}>{uploadInfo.errors.join(" ")}</span>}
                </div>
              )}
            </div>
          )}
        </div>
        {hasUploaded && (
          <div className={`px-4 py-3 border-t ${divider} shrink-0`}>
            <button onClick={() => setConfirm({ title: "Clear all data?", body: "This removes every loaded product, forecast and test result. It can't be undone — you'd need to upload your file again.", confirmLabel: "Clear everything", danger: true, onConfirm: handleReset })}
              className={`w-full text-[11px] py-1.5 rounded-lg transition-colors border ${resetBtn}`}>
              Clear all data
            </button>
          </div>
        )}
        <div onMouseDown={startResizeWidth} title="Drag to resize sidebar"
          className={`absolute top-0 right-0 h-full w-1.5 cursor-col-resize z-30 ${lm ? "hover:bg-violet-300" : "hover:bg-violet-600/50"}`} />
      </div>
      )}

      {/* Main area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <div className={`h-14 border-b ${divider} ${headerBg} flex items-center px-4 gap-2 shrink-0`}>
          <div className="flex items-center gap-2 mr-1 shrink-0">
            <div className="h-7 w-7 rounded-lg flex items-center justify-center shrink-0" style={{ background:"linear-gradient(135deg,#8b7dff,#6c5cff)", boxShadow:"0 4px 14px rgba(108,92,255,.45)" }}>
              <span className="text-white text-[13px] font-bold">L</span>
            </div>
            <span className={`text-[15px] font-bold ${logoText} tracking-tight hidden md:block`}>LogiTrack</span>
          </div>
          <nav className="flex items-center gap-0.5 shrink-0">
            {[["fleet","Fleet"],["suppliers","Suppliers"],["scorecard","Scorecard"],["backtest","Backtest"],["live","Live accuracy"],["categorize","Grouping"]].map(([v,label]) => (
              <button key={v} onClick={() => { setActiveSku(null); setActiveView(v); }}
                className={`px-2.5 py-1 rounded-lg text-[13px] font-semibold transition-all ${activeSku===null && activeView===v ? (lm ? "bg-violet-100 text-violet-700" : "bg-violet-500/15 text-violet-300") : (lm ? "text-slate-500 hover:bg-slate-100" : "text-slate-400 hover:bg-slate-800/60")}`}>
                {label}
              </button>
            ))}
          </nav>
          <button onClick={() => setShowProducts(v => !v)} title="Products & import"
            className={`shrink-0 flex items-center gap-1.5 h-8 px-3 rounded-lg text-[13px] font-semibold border transition-colors ${lm ? "border-slate-200 text-slate-600 hover:bg-slate-100" : "border-slate-700/60 text-slate-300 hover:bg-slate-800/60"}`}>
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 6h16M4 12h16M4 18h16" /></svg>
            Products
          </button>
          {activeSku ? (
            <>
              <button onClick={() => setActiveSku(null)}
                className={`text-xs flex items-center gap-1.5 transition-colors ${lm ? "text-slate-500 hover:text-slate-800" : "text-slate-500 hover:text-slate-300"}`}>
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                </svg>
                Fleet
              </button>
              <span className={breadcrumbSep}>/</span>
              {(() => {
                const folderEntry = Object.entries(folders).find(([, f]) => f.skuIds.includes(activeSku));
                return folderEntry ? (
                  <>
                    <span className={`text-xs ${breadcrumbMuted} flex items-center gap-1`}>
                      <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
                      </svg>
                      {folderEntry[1].name}
                    </span>
                    <span className={breadcrumbSep}>/</span>
                  </>
                ) : null;
              })()}
              <span title={skuList.find(s => s.id === activeSku)?.name || activeSku}
                className={`text-xs font-semibold whitespace-nowrap truncate max-w-[220px] shrink-0 ${sectionTitleText}`}>{skuList.find(s => s.id === activeSku)?.name || activeSku}</span>
              <div className="sku-strip flex items-center gap-1 ml-4 min-w-0 overflow-x-auto pb-1">
                {skuList.map(s => (
                  <button key={s.id} onClick={() => setActiveSku(s.id)}
                    className={`px-3 py-1 rounded-lg text-xs font-mono transition-all whitespace-nowrap ${activeSku === s.id ? skuTabActive : skuTabInactive}`}>
                    {s.id}
                  </button>
                ))}
              </div>
            </>
          ) : null}
          <div className="ml-auto flex items-center gap-2.5">
            <button onClick={() => { setPaletteQ(""); setPalette(true); }} title="Search (⌘K)"
              className={`hidden sm:flex items-center gap-2 h-8 px-3 rounded-lg text-[12.5px] border transition-colors ${lm ? "border-slate-200 text-slate-400 hover:bg-slate-100" : "border-slate-700/60 text-slate-400 hover:bg-slate-800/60"}`}>
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" /></svg>
              <span>Search</span>
              <span className={`ml-1 text-[10px] font-mono px-1.5 py-0.5 rounded border ${lm ? "border-slate-200 text-slate-400" : "border-slate-700 text-slate-500"}`}>⌘K</span>
            </button>
            <ThemeToggle />
            <span className={`text-[11px] ${dateText} font-mono`}>{new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}</span>
          </div>
        </div>

        <div className="flex-1 overflow-hidden flex flex-col">
          {/* "live" is exempt like suppliers/backtest: the forecast log persists across
              restarts while the catalogue doesn't, so you can legitimately have history to
              read before any products are loaded. */}
          {skuList.length === 0 && activeView !== "suppliers" && activeView !== "backtest" && activeView !== "live" ? (
            <div className="flex-1 flex flex-col items-center justify-center text-center px-8">
              <div className={`h-16 w-16 rounded-2xl flex items-center justify-center mb-5 ${lm ? "bg-violet-100" : "bg-violet-950/40"}`}>
                <svg className={`h-7 w-7 ${lm ? "text-violet-600" : "text-violet-400"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.9A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                </svg>
              </div>
              <h2 className={`text-lg font-bold ${lm ? "text-slate-900" : "text-white"}`}>No products loaded</h2>
              <p className={`text-sm mt-2 max-w-md ${lm ? "text-slate-500" : "text-slate-400"}`}>
                Upload a sales file following the format to get started. Your file needs <span className="font-semibold">Date</span> and <span className="font-semibold">Units_Sold</span> columns — optional: SKU, Category, Price, On_Promotion, Units_In_Stock.
              </p>
              <p className={`text-xs mt-3 ${lm ? "text-slate-400" : "text-slate-500"}`}>
                Click <span className="font-semibold">Products</span> in the top bar to import a file or download the template.
              </p>
            </div>
          ) : activeSku ? (
            <SkuDetailPanel skuId={activeSku} skuList={skuList} params={getParams(activeSku)}
              onParamChange={p => updateParams(activeSku, p)} openPOs={openPOs} setOpenPOs={setOpenPOs}
              apiKey={GROQ_API_KEY} lm={lm} suppliers={suppliers} setSuppliers={setSuppliers} receiveStock={receiveStock}
              refreshNonce={refreshNonce} onExport={() => exportSku(activeSku)} exporting={exporting === "sku"}
              arrival={arrivals.find(a => a.skuId === activeSku) || null}
              onConfirmArrival={confirmArrival} onIgnoreArrival={ignoreArrival} />
          ) : activeView === "suppliers" ? (
            <SupplierPanel suppliers={suppliers} setSuppliers={setSuppliers} skuList={skuList} lm={lm} openPOs={openPOs} setOpenPOs={setOpenPOs} receiveStock={receiveStock}
              onExport={exportSuppliers} exporting={exporting === "suppliers"}
              pendingArrival={pendingArrival} onArrivalHandled={() => { setPendingArrival(null); refreshArrivals(); }}
              skuParams={skuParams} />
          ) : activeView === "scorecard" ? (
            <div className="flex-1 overflow-y-auto">
              <ScorecardTab api={API} skuList={skuList} skuParams={skuParams}
                setSkuParams={setSkuParams} openPOs={openPOs} lm={lm} folders={folders}
                suppliers={suppliers} />
            </div>
          ) : activeView === "backtest" ? (
            <BacktestTab api={API} lm={lm} apiKey={GROQ_API_KEY} skuParams={skuParams}
              holdingPct={holdingPct} setHoldingPct={setHoldingPct} res={btRes} setRes={setBtRes} diag={btDiag} setDiag={setBtDiag}
              waiting={btWaiting.map(id => skuList.find(s => s.id === id)?.name || id)} waitingIds={btWaiting}
              onPickSku={(id) => { setActiveSku(id); setActiveView("fleet"); }} />
          ) : activeView === "live" ? (
            <div className="flex-1 overflow-y-auto p-4 sm:p-6">
              <LiveAccuracy api={API} lm={lm}
                onPickSku={(id) => { setActiveSku(id); setActiveView("fleet"); }} />
            </div>
          ) : activeView === "categorize" ? (
            <CategorizePanel embedded api={API} skuList={skuList} apiKey={GROQ_API_KEY} lm={lm} onApplied={onCatalogChanged} scorecardRows={scorecardRows} />
          ) : (
            <div className="flex-1 overflow-y-auto">
              {arrivals.length > 0 && (
                <div className="px-4 sm:px-6 pt-4 sm:pt-6">
                  <ArrivalPromptList found={arrivals} skuList={skuList} lm={lm}
                    onConfirm={confirmArrival} onIgnore={ignoreArrival} />
                </div>
              )}
              <FleetBento skuForecasts={skuForecasts} getParams={getParams}
                openPOs={openPOs} onSelectSku={setActiveSku} lm={lm}
                onExportFleet={exportFleet} onExportAll={exportAll} exporting={exporting}
                scorecardRows={scorecardRows} />
            </div>
          )}
        </div>
      </div>

      <ErrorToasts lm={lm} />

      {/* A backtest can take minutes. Without a visible marker the dashboard looks
          finished while the protection levels are still provisional. */}
      {btJob?.status === "running" && (
        <div className={`fixed bottom-4 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2.5 px-4 py-2.5 rounded-full border shadow-lg text-[12px] ${lm ? "bg-white border-violet-200 text-slate-700" : "bg-[#0d1117] border-violet-900/50 text-slate-200"}`}>
          <svg className={`h-3.5 w-3.5 animate-spin ${lm ? "text-violet-600" : "text-violet-400"}`} fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
          </svg>
          <span>
            <span className="font-semibold">Testing your protection levels against your own sales history…</span>
            <span className={`ml-1.5 ${lm ? "text-slate-500" : "text-slate-400"}`}>
              a few minutes · figures shown are provisional estimates until it finishes
            </span>
          </span>
        </div>
      )}
      {btJob?.status === "error" && (
        <div className={`fixed bottom-4 left-1/2 -translate-x-1/2 z-40 px-4 py-2.5 rounded-full border shadow-lg text-[12px] ${lm ? "bg-rose-50 border-rose-200 text-rose-700" : "bg-rose-950/40 border-rose-900/50 text-rose-300"}`}>
            <span className="font-semibold">Protection test failed.</span> Still using estimates. <span className="font-mono opacity-80">{btJob.error}</span>
            <button onClick={() => triggerBacktest("retry")} className="ml-2 underline font-semibold">Retry</button>
        </div>
      )}

      <AiDrawer skuForecasts={skuForecasts} skuParams={skuParams} skuList={skuList}
        openPOs={openPOs} folders={folders} apiKey={GROQ_API_KEY} suppliers={suppliers} api={API} />

      <CategorizePanel skuList={skuList} api={API} apiKey={GROQ_API_KEY} lm={lm} onApplied={onCatalogChanged}
        scorecardRows={scorecardRows}
        open={showCategorize} onClose={() => setShowCategorize(false)} />

      {assignModal && (
        <AssignFolderModal skuId={assignModal.skuId} skuName={assignModal.skuName} folders={folders}
          onAssign={(folderId) => assignSkuToFolder(folderId, assignModal.skuId)}
          onClose={() => setAssignModal(null)} lm={lm} />
      )}

      {confirm && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setConfirm(null)}>
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
          <div onClick={e => e.stopPropagation()}
            className={`relative w-full max-w-sm rounded-2xl border shadow-2xl p-5 ${lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10"}`}
            style={{ boxShadow: "0 24px 70px rgba(0,0,0,.5)" }}>
            <div className={`text-base font-bold ${lm ? "text-slate-900" : "text-white"}`}>{confirm.title}</div>
            {confirm.body && <p className={`text-[13px] mt-2 leading-relaxed ${lm ? "text-slate-600" : "text-slate-400"}`}>{confirm.body}</p>}
            <div className="flex items-center justify-end gap-2 mt-5">
              <button onClick={() => setConfirm(null)}
                className={`px-3.5 py-2 rounded-lg text-[13px] font-semibold transition-colors ${lm ? "text-slate-600 hover:bg-slate-100" : "text-slate-300 hover:bg-white/5"}`}>Cancel</button>
              <button onClick={() => { const fn = confirm.onConfirm; setConfirm(null); fn?.(); }}
                className={`px-3.5 py-2 rounded-lg text-[13px] font-bold text-white transition-colors ${confirm.danger ? "bg-rose-600 hover:bg-rose-500" : "bg-violet-600 hover:bg-violet-500"}`}>{confirm.confirmLabel || "Confirm"}</button>
            </div>
          </div>
        </div>
      )}

      {palette && (() => {
        const q = paletteQ.trim().toLowerCase();
        const skuHits = (q
          ? skuList.filter(s => (s.id + " " + (s.name || "")).toLowerCase().includes(q))
          : skuList).slice(0, 8);
        const navHits = [
          ["Fleet overview", "fleet", "📊"], ["Suppliers", "suppliers", "🏭"],
          ["Scorecard", "scorecard", "🎯"], ["Backtest", "backtest", "🧪"], ["Product grouping", "categorize", "🗂️"],
        ].filter(([label]) => !q || label.toLowerCase().includes(q));
        const go = () => { const f = skuHits[0]; if (f) { setActiveSku(f.id); setPalette(false); } else if (navHits[0]) { setActiveSku(null); setActiveView(navHits[0][1]); setPalette(false); } };
        return (
          <div className="fixed inset-0 z-[60] flex items-start justify-center pt-[14vh] px-4"
            onClick={() => setPalette(false)}>
            <div className="absolute inset-0 bg-black/55 backdrop-blur-sm" />
            <div onClick={e => e.stopPropagation()}
              className={`relative w-full max-w-xl rounded-2xl border shadow-2xl overflow-hidden ${lm ? "bg-white border-slate-200" : "bg-[#161619] border-white/10"}`}
              style={{ boxShadow: "0 30px 90px rgba(0,0,0,.6)" }}>
              <div className={`flex items-center gap-3 px-4 h-14 border-b ${lm ? "border-slate-100" : "border-white/10"}`}>
                <svg className={`h-4 w-4 shrink-0 ${lm ? "text-slate-400" : "text-slate-500"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z" /></svg>
                <input autoFocus value={paletteQ} onChange={e => setPaletteQ(e.target.value)}
                  onKeyDown={e => { if (e.key === "Enter") go(); }}
                  placeholder="Jump to a SKU, supplier, or view…"
                  className={`flex-1 bg-transparent outline-none text-[15px] ${lm ? "text-slate-900 placeholder-slate-400" : "text-white placeholder-slate-500"}`} />
                <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${lm ? "border-slate-200 text-slate-400" : "border-slate-700 text-slate-500"}`}>esc</span>
              </div>
              <div className="max-h-[52vh] overflow-y-auto py-2">
                {skuHits.length > 0 && (
                  <div className={`px-4 pt-1 pb-1 text-[11px] font-semibold uppercase tracking-wide ${lm ? "text-slate-400" : "text-slate-500"}`}>Products</div>
                )}
                {skuHits.map(s => (
                  <button key={s.id} onClick={() => { setActiveSku(s.id); setPalette(false); }}
                    className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${lm ? "hover:bg-slate-100" : "hover:bg-white/5"}`}>
                    <span className={`text-[11px] font-mono px-1.5 py-0.5 rounded ${lm ? "bg-slate-100 text-slate-500" : "bg-white/5 text-slate-400"}`}>{s.id}</span>
                    <span className={`text-[14px] ${lm ? "text-slate-800" : "text-slate-200"}`}>{s.name || s.id}</span>
                  </button>
                ))}
                {navHits.length > 0 && (
                  <div className={`px-4 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wide ${lm ? "text-slate-400" : "text-slate-500"}`}>Views</div>
                )}
                {navHits.map(([label, view, icon]) => (
                  <button key={view} onClick={() => { setActiveSku(null); setActiveView(view); setPalette(false); }}
                    className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${lm ? "hover:bg-slate-100" : "hover:bg-white/5"}`}>
                    <span className="text-[15px]">{icon}</span>
                    <span className={`text-[14px] ${lm ? "text-slate-800" : "text-slate-200"}`}>{label}</span>
                  </button>
                ))}
                {skuHits.length === 0 && navHits.length === 0 && (
                  <div className={`px-4 py-8 text-center text-[13px] ${lm ? "text-slate-400" : "text-slate-500"}`}>No matches for “{paletteQ}”.</div>
                )}
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}