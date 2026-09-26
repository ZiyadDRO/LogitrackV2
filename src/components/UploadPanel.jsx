import React, { useState, useEffect, useRef, useCallback } from 'react';
import HoldingWizard from './HoldingWizard';
import { API } from '../lib/api';

// ─── UPLOAD PANEL ─────────────────────────────
//
// Two ways in: a spreadsheet, or a live store. The store side is driven by /api/sources
// rather than hardcoding one form per platform — a new connector shows up here with a
// working credentials form and a working saved-account row without this file changing.
//
// Saved accounts exist so a token is pasted once. The browser NEVER holds the token: the
// server returns a masked hint (EAAA••••7Xk2) and an id, and an import sends the id.

export default function UploadPanel({ onUploadSuccess, onUploadError, isUploading, setIsUploading, lm,
                                     holdingPct = 25, setHoldingPct = null,
                                     uplift = null, availCounts = null }) {
  const fileRef = useRef(null);
  const [mode, setMode] = useState("file");          // file | store
  const [days, setDays] = useState("");   // blank = pull all available history
  // ON by default. A second upload used to silently wipe the first, which is never what
  // you want when you're building up a catalog file by file. Everything is modelled as
  // one catalog; the backtest still reports each file's products separately.
  const [addToExisting, setAddToExisting] = useState(true);
  const muted = "text-[var(--t-dim)]";
  const [showWizard, setShowWizard] = useState(false);

  // ── Live store connections ──────────────────────────────────────────────
  const [sources, setSources] = useState([]);         // [{key,label,fields,help}]
  const [connections, setConnections] = useState([]);
  const [storeView, setStoreView] = useState("list"); // list | new
  const [draftSource, setDraftSource] = useState(null);
  const [creds, setCreds] = useState({});
  const [label, setLabel] = useState("");
  const [saveAccount, setSaveAccount] = useState(true);
  const [testing, setTesting] = useState(false);
  const [locations, setLocations] = useState(null);   // null = not checked yet
  const [pickedLocations, setPickedLocations] = useState([]);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [notice, setNotice] = useState(null);
  const [sync, setSync] = useState(null);             // nightly sync status
  const [syncing, setSyncing] = useState(false);

  const loadSync = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/sync`);
      if (res.ok) setSync(await res.json());
    } catch { /* the panel works fine without it */ }
  }, []);

  const loadConnections = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/connections`);
      if (!res.ok) return;
      const data = await res.json();
      const list = data.connections || [];
      setConnections(list);
      // Land on the form when there's nothing saved yet — one less click on first run.
      if (!list.length) setStoreView("new");
    } catch { /* offline server: the file tab still works */ }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${API}/api/sources`);
        if (res.ok) {
          const data = await res.json();
          const list = data.sources || [];
          setSources(list);
          if (list.length) setDraftSource(prev => prev || list[0].key);
        }
      } catch { /* leave the store tab empty rather than breaking the panel */ }
      loadConnections();
      loadSync();
    })();
  }, [loadConnections, loadSync]);

  const activeSource = sources.find(s => s.key === draftSource) || null;
  const missingRequired = (activeSource?.fields || [])
    .filter(f => f.required && !String(creds[f.name] ?? "").trim());

  const resetDraft = () => {
    setCreds({}); setLabel(""); setLocations(null); setPickedLocations([]); setNotice(null);
  };

  const credBody = () => {
    const out = { ...creds };
    if (pickedLocations.length) out.locationIds = pickedLocations;
    return out;
  };

  // ── Check credentials without importing ─────────────────────────────────
  // Finding out a token is wrong halfway through a two-year pull that already replaced
  // your catalog is a bad way to find out. For Square this also populates the location
  // picker, which is the only way to know what the location ids are.
  const handleTest = async (connectionId = null) => {
    setTesting(true); setNotice(null); onUploadError(null);
    try {
      const body = connectionId ? { connectionId } : { source: draftSource, creds: credBody() };
      const res = await fetch(`${API}/api/connections/test`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Could not reach the store.");
      if (data.locations) {
        setLocations(data.locations);
        const active = data.locations.filter(l => (l.status || "ACTIVE") === "ACTIVE");
        setPickedLocations(active.map(l => l.id));
        setNotice(`Connected. ${active.length} active location${active.length === 1 ? "" : "s"} found.`);
      } else {
        setNotice(`Connected. ${data.products ?? 0} products visible.`);
      }
    } catch (err) {
      onUploadError(err.message || "Network error. Is the server running?");
    } finally { setTesting(false); }
  };

  const runImport = async (body) => {
    setIsUploading(true); onUploadError(null); setNotice(null);
    try {
      const res = await fetch(`${API}/api/upload/source`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: days.trim() ? Number(days) : null, ...body }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Import failed.");
      await loadConnections();
      loadSync();
      if (data.connection) { setStoreView("list"); resetDraft(); }
      onUploadSuccess(data);
    } catch (err) {
      onUploadError(err.message || "Network error. Is the server running?");
    } finally { setIsUploading(false); }
  };

  const handleConnectImport = () => {
    if (missingRequired.length) {
      onUploadError(`Enter ${missingRequired.map(f => f.label.toLowerCase()).join(" and ")}.`);
      return;
    }
    runImport({
      source: draftSource, creds: credBody(),
      save: saveAccount, label: label.trim() || undefined,
    });
  };

  const handleDeleteConnection = async (id) => {
    try {
      await fetch(`${API}/api/connections/${id}`, { method: "DELETE" });
      setConfirmDelete(null);
      loadConnections();
    } catch { /* a failed delete just leaves the row in place */ }
  };

  const handleFile = async (file, addMode = addToExisting) => {
    if (!file) return;
    setIsUploading(true);
    onUploadError(null);
    const form = new FormData();
    form.append("file", file);
    try {
      // append merges on TRUE calendar dates, so a second file continues the first
      // rather than replacing it and re-anchoring the timeline.
      const res = await fetch(`${API}/api/upload?mode=${addMode ? "append" : "replace"}`,
        { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Upload failed.");
      onUploadSuccess(data);
    } catch (err) {
      onUploadError(err.message || "Network error. Is the server running?");
    } finally {
      setIsUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const dropzone = lm
    ? "border-2 border-dashed border-[var(--t-line2)] rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group bg-[var(--t-sunken)]"
    : "border-2 border-dashed border-[var(--t-line2)] rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group";
  const iconBox = lm
    ? "h-8 w-8 rounded-xl bg-[var(--t-panel)] border border-[var(--t-line)] group-hover:border-[var(--t-accent-line)] flex items-center justify-center transition-colors text-[var(--t-dim)] group-hover:text-[var(--t-accent)]"
    : "h-8 w-8 rounded-xl bg-[var(--t-line)] group-hover:bg-[var(--t-line)] flex items-center justify-center transition-colors text-[var(--t-dim)] group-hover:text-[var(--t-accent)]";

  const tabBtn = (m, text) => (
    <button onClick={() => setMode(m)}
      className={`flex-1 text-[14px] font-semibold py-1.5 rounded-md transition-colors ${mode === m
        ? ("bg-[var(--t-panel)] text-[var(--t-ink)] shadow-sm")
        : ("text-[var(--t-dim)] hover:text-[var(--t-soft)]")}`}>{text}</button>
  );
  const fld = lm
    ? "w-full bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-lg px-2.5 py-2 text-[15px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]"
    : "w-full bg-[#161619] border border-[var(--t-line)] rounded-lg px-2.5 py-2 text-[15px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]";
  const btn = "w-full rounded-lg py-2 text-[15px] font-bold text-[var(--t-ink)] bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] disabled:opacity-60 transition-colors";
  const ghostBtn = "text-[14px] font-semibold text-[var(--t-accent)] hover:underline";

  const sourceLabel = (key) => sources.find(s => s.key === key)?.label || key;

  const historyRow = (
    <div className="flex items-center gap-2">
      <span className={`text-[14px] ${muted}`}>History</span>
      <input value={days} onChange={e => setDays(e.target.value.replace(/[^0-9]/g, ""))}
        placeholder="all" className={`${fld} !w-24`} />
      <span className={`text-[14px] ${muted}`}>days (blank = all)</span>
    </div>
  );

  // ── One saved account row ───────────────────────────────────────────────
  const connectionRow = (c) => (
    <div key={c.id}
      className={`rounded-lg border px-2.5 py-2 ${"bg-[var(--t-panel)] border-[var(--t-line)]"}`}>
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className={`text-[15px] font-semibold truncate ${"text-[var(--t-soft)]"}`}>{c.label}</span>
            <span className={`text-[12px] px-1.5 py-0.5 rounded font-semibold shrink-0 ${"bg-[var(--t-sunken)] text-[var(--t-dim)]"}`}>
              {sourceLabel(c.source)}
            </span>
          </div>
          <div className={`text-[13px] font-mono ${muted}`}>
            {c.secretHint || "saved"}
            {c.creds?.environment === "sandbox" ? " · sandbox" : ""}
            {c.creds?.locationIds?.length
              ? ` · ${c.creds.locationIds.length} location${c.creds.locationIds.length === 1 ? "" : "s"}`
              : ""}
          </div>
        </div>
        <button onClick={() => runImport({ connectionId: c.id })} disabled={isUploading}
          className={`text-[14px] font-bold px-2.5 py-1 rounded-lg shrink-0 disabled:opacity-60 ${"bg-[var(--t-accent-soft)] text-[var(--t-ink)]"}`}>
          {isUploading ? "…" : "Import"}
        </button>
        {confirmDelete === c.id ? (
          <button onClick={() => handleDeleteConnection(c.id)}
            className={`text-[13px] font-semibold px-1.5 shrink-0 ${"text-[var(--t-bad)]"}`}>Remove?</button>
        ) : (
          <button onClick={() => setConfirmDelete(c.id)} title="Forget this account"
            className={`text-[16px] leading-none px-1 shrink-0 ${muted} hover:text-[var(--t-bad)]`}>×</button>
        )}
      </div>
    </div>
  );

  // ── One credential field, rendered from the server's own spec ───────────
  const credField = (f) => {
    if (f.type === "select") {
      return (
        <select key={f.name} value={creds[f.name] ?? f.default ?? ""} className={fld}
          onChange={e => { setCreds({ ...creds, [f.name]: e.target.value }); setLocations(null); }}>
          {(f.options || []).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      );
    }
    if (f.type === "locations") {
      // Not a text input: nobody can type a Square location id from memory. It appears
      // once the credentials have been checked, and defaults to every active location.
      if (!locations) return null;
      return (
        <div key={f.name} className={`rounded-lg border px-2.5 py-2 space-y-1 ${"bg-[var(--t-panel)] border-[var(--t-line)]"}`}>
          <div className={`text-[14px] font-semibold ${"text-[var(--t-soft)]"}`}>{f.label}</div>
          {locations.map(l => {
            const on = pickedLocations.includes(l.id);
            return (
              <label key={l.id} className="flex items-center gap-2 text-[14px] cursor-pointer">
                <input type="checkbox" checked={on}
                  onChange={() => setPickedLocations(on
                    ? pickedLocations.filter(x => x !== l.id)
                    : [...pickedLocations, l.id])} />
                <span className={"text-[var(--t-soft)]"}>{l.name}</span>
                <span className={`text-[13px] ${muted}`}>{l.timezone}</span>
                {(l.status && l.status !== "ACTIVE") && (
                  <span className={`text-[12px] ${muted}`}>· {l.status.toLowerCase()}</span>
                )}
              </label>
            );
          })}
          {pickedLocations.length > 1 && (
            <p className={`text-[13px] leading-relaxed ${muted}`}>
              Sales from all ticked locations are pooled into one figure per product. That
              works when stock moves freely between them, but hides a single-store stockout
              when it doesn&apos;t. Per-location forecasting isn&apos;t built yet.
            </p>
          )}
        </div>
      );
    }
    return (
      <input key={f.name} value={creds[f.name] ?? ""} type={f.type === "password" ? "password" : "text"}
        placeholder={f.placeholder || f.label} className={fld}
        onChange={e => { setCreds({ ...creds, [f.name]: e.target.value }); setLocations(null); }} />
    );
  };

  return (
    <div className="space-y-2.5">
      <div className={`flex gap-1 p-0.5 rounded-lg ${"bg-[var(--t-sunken)]"}`}>
        {tabBtn("file", "Upload file")}{tabBtn("store", "Connect store")}
      </div>

      {mode === "store" ? (
        <div className="space-y-2">
          {storeView === "list" ? (
            <>
              {connections.length > 0 && (
                <div className="space-y-1.5">{connections.map(connectionRow)}</div>
              )}
              {historyRow}
              {/* Whether the tool is keeping itself current, in one line. Without this the
                  nightly sync is invisible: the page looks live either way, and the only
                  hint that it stopped a week ago is demand that never moves. */}
              {sync && (
                <div className={`rounded-lg border px-2.5 py-2 space-y-1 ${"bg-[var(--t-panel)] border-[var(--t-line)]"}`}>
                  <div className="flex items-center gap-2">
                    <span className={`text-[14px] font-semibold ${"text-[var(--t-soft)]"}`}>
                      {sync.enabled
                        ? `Syncs nightly at ${sync.at}${sync.nextRunAt ? ` · next ${new Date(sync.nextRunAt).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}`
                        : "Nightly sync is off"}
                    </span>
                    <button onClick={async () => {
                        setSyncing(true); onUploadError(null);
                        try {
                          const r = await fetch(`${API}/api/sync/now`, { method: "POST" });
                          const d = await r.json();
                          if (d?.ok === false) onUploadError(d.error || d.result?.detail || "Sync failed.");
                          await loadSync();
                        } catch { onUploadError("Network error. Is the server running?"); }
                        finally { setSyncing(false); }
                      }} disabled={syncing || isUploading} className={`ml-auto ${ghostBtn} disabled:opacity-60`}>
                      {syncing ? "syncing…" : "sync now"}
                    </button>
                  </div>
                  <div className={`text-[13px] ${muted}`}>
                    {sync.lastRun
                      ? (sync.lastRun.ok
                          ? `Last sync ${new Date(sync.lastRun.finishedAt).toLocaleString()} · ${sync.lastRun.result?.skus ?? 0} products`
                          : `Last sync failed: ${sync.lastRun.error || sync.lastRun.result?.detail || "unknown reason"}`)
                      : "Hasn't run yet."}
                  </div>
                  {sync.enabled && !sync.connection && (
                    <div className={`text-[13px] ${"text-[var(--t-warn)]"}`}>
                      Nothing to sync from. Tick &quot;Remember this account&quot; when you connect.
                    </div>
                  )}
                  {/* THE SECOND CLOCK. Stock is read hourly, and unlike the nightly sync a
                      missed reading is gone for good — nobody records what your stock was at
                      2pm last Tuesday. So a sampler that quietly died needs to be visible the
                      next morning, not discovered a week later as a hole in the record. */}
                  {sync.sampler && (
                    <div className={`pt-1.5 mt-1.5 border-t ${"border-[var(--t-line)]"}`}>
                      <div className="flex items-center gap-2">
                        {/* Grey when nothing has been filed yet — green would claim readings
                            are landing when none have. Red is reserved for genuine failures;
                            "no store connected" is a skip and never reaches here. */}
                        <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                          !sync.sampler.enabled ? "bg-[var(--t-line2)]"
                          : sync.sampler.consecutiveFailures > 0 ? "bg-[var(--t-bad)]"
                          : !sync.sampler.lastFiledAt ? "bg-[var(--t-line2)]"
                          : "bg-[var(--t-good)]"
                        }`} />
                        <span className={`text-[13px] ${muted}`}>
                          {!sync.sampler.enabled
                            ? "Hourly stock readings are off"
                            : sync.sampler.lastFiledAt
                              ? `Stock & today's sales read hourly · last ${new Date(sync.sampler.lastFiledAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
                              : "Stock & today's sales read hourly · nothing filed yet"}
                          {/* The schedule itself, so it can be seen rather than trusted. A
                              manual "read now" never moves this time. */}
                          {sync.sampler.enabled && sync.sampler.nextScheduledAt && (
                            <> · next {new Date(sync.sampler.nextScheduledAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</>
                          )}
                        </span>
                        <button onClick={async () => {
                            try { await fetch(`${API}/api/sync/sample-now`, { method: "POST" }); await loadSync(); }
                            catch { onUploadError("Network error. Is the server running?"); }
                          }} disabled={isUploading} className={`ml-auto ${ghostBtn} disabled:opacity-60`}>
                          read now
                        </button>
                      </div>
                      {sync.sampler.consecutiveFailures > 0 && (
                        <div className={`text-[13px] mt-0.5 ${"text-[var(--t-bad)]"}`}>
                          {sync.sampler.consecutiveFailures} reading{sync.sampler.consecutiveFailures === 1 ? "" : "s"} failed in a row
                          {sync.sampler.lastAttempt?.result?.reason ? `: ${sync.sampler.lastAttempt.result.reason}` : ""}.
                          {" "}Hours missed while this is broken cannot be recovered.
                        </div>
                      )}
                      {sync.sampler.coverage?.samples > 0 && (
                        <div className={`text-[13px] mt-0.5 ${muted}`}>
                          {sync.sampler.coverage.samples.toLocaleString()} readings over {sync.sampler.coverage.daysCovered} day
                          {sync.sampler.coverage.daysCovered === 1 ? "" : "s"}
                          {availCounts?.unsampled > 0 && ` · ${availCounts.unsampled} product${availCounts.unsampled === 1 ? "" : "s"} not yet covered`}
                        </div>
                      )}
                      {/* What the readings BOUGHT. Hourly sampling exists to catch the days a
                          product ran out partway through, so demand on those days can be
                          corrected upward instead of teaching the forecast that sales are
                          falling. This is the line that says whether that ever happened —
                          and a long run of zero is itself an answer worth having. */}
                      {uplift && (
                        <div className={`text-[13px] mt-0.5 ${uplift.productDays > 0 ? "text-[var(--t-good)]" : muted}`}>
                          {uplift.productDays > 0
                            ? `Corrected ${uplift.productDays} sold-out day${uplift.productDays === 1 ? "" : "s"} across `
                              + `${uplift.skus} product${uplift.skus === 1 ? "" : "s"}, adding ${uplift.unitsAdded} units of demand `
                              + "that would have looked like a slowdown."
                            : "No mid-day sellouts corrected yet. Either nothing sold out partway through a day, or there aren't enough readings to tell."}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              <button onClick={() => { resetDraft(); setStoreView("new"); }} className={btn}>
                + Connect another store
              </button>
              <p className={`text-[14px] leading-relaxed ${muted}`}>
                Importing replaces the catalog with what the store reports. Saved tokens stay
                on the server; this page only sees the masked hint above. Nightly sync only
                runs while the backend is running.
              </p>
            </>
          ) : (
            <>
              {sources.length > 1 && (
                <div className={`flex gap-1 p-0.5 rounded-lg ${"bg-[var(--t-sunken)]"}`}>
                  {sources.map(s => (
                    <button key={s.key}
                      onClick={() => { setDraftSource(s.key); resetDraft(); }}
                      className={`flex-1 text-[14px] font-semibold py-1.5 rounded-md transition-colors ${draftSource === s.key
                        ? "bg-[var(--t-panel)] text-[var(--t-ink)] shadow-sm"
                        : "text-[var(--t-dim)] hover:text-[var(--t-soft)]"}`}>{s.label}</button>
                  ))}
                </div>
              )}

              {(activeSource?.fields || []).map(credField)}

              <input value={label} onChange={e => setLabel(e.target.value)}
                placeholder="Name this account (optional)" className={fld} />

              {historyRow}

              {notice && (
                <p className={`text-[14px] font-semibold ${"text-[var(--t-accent)]"}`}>{notice}</p>
              )}

              <div className="flex gap-2">
                <button onClick={() => handleTest()} disabled={testing || isUploading || missingRequired.length > 0}
                  className={`flex-1 rounded-lg py-2 text-[15px] font-semibold border disabled:opacity-60 transition-colors ${"text-[var(--t-accent)] border-[var(--t-line2)] hover:border-[var(--t-accent-line)]"}`}>
                  {testing ? "Checking…" : "Check connection"}
                </button>
                <button onClick={handleConnectImport} disabled={isUploading || missingRequired.length > 0}
                  className={`${btn} flex-1`}>
                  {isUploading ? "Importing…" : "Connect & import"}
                </button>
              </div>

              <label className="flex items-center gap-2 text-[14px] cursor-pointer">
                <input type="checkbox" checked={saveAccount}
                  onChange={e => setSaveAccount(e.target.checked)} />
                <span className={"text-[var(--t-soft)]"}>Remember this account</span>
                <span className={muted}>(so you don&apos;t re-enter the token)</span>
              </label>

              {activeSource?.help && (
                <p className={`text-[14px] leading-relaxed ${muted}`}>{activeSource.help}</p>
              )}

              {connections.length > 0 && (
                <button onClick={() => { setStoreView("list"); resetDraft(); }} className={ghostBtn}>
                  ← Back to saved accounts
                </button>
              )}
            </>
          )}
        </div>
      ) : (
      <>
      <div onDrop={e => { e.preventDefault(); handleFile(e.dataTransfer.files[0]); }} onDragOver={e => e.preventDefault()} onClick={() => fileRef.current?.click()} className={dropzone}>
        <div className={iconBox}>
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
          </svg>
        </div>
        <span className={`text-[15px] font-semibold text-center ${"text-[var(--t-soft)]"}`}>{isUploading ? "Uploading..." : "Drop Excel file or click to browse"}</span>
        <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>.xlsx or .xls · multi-SKU supported</span>
        <input ref={fileRef} type="file" accept=".xlsx,.xls,.csv" className="hidden" onChange={e => handleFile(e.target.files[0])} />
      </div>
      <label onClick={(e) => e.stopPropagation()}
        className={`flex items-start gap-2 text-[14px] cursor-pointer rounded-lg px-2.5 py-2 border transition-colors ${addToExisting
          ? ("bg-[var(--t-accent-soft)] border-[var(--t-accent-line)]")
          : ("bg-[var(--t-panel)] border-[var(--t-line)] hover:border-[var(--t-line2)]")}`}>
        <input type="checkbox" checked={addToExisting} onChange={(e) => setAddToExisting(e.target.checked)} className="mt-0.5" />
        <span>
          <span className={`font-semibold ${"text-[var(--t-soft)]"}`}>Add to what&apos;s already loaded</span>
          <span className={`block ${"text-[var(--t-dim)]"}`}>
            {addToExisting
              ? "New products are added. Products already loaded get this file's rows merged into their history by date. Everything is forecast and tested as one catalog; the Backtest tab reports each file separately."
              : <span className={"text-[var(--t-bad)]"}>Off: this file will REPLACE everything currently loaded.</span>}
          </span>
        </span>
      </label>
      {/* Holding rate belongs HERE, not only in the Backtest tab: a backtest starts the
          moment you upload, so a client who knows their real figure must be able to set
          it beforehand rather than getting a run on 25% and needing another. */}
      <label onClick={(e) => e.stopPropagation()}
        className={`flex items-center gap-2 text-[14px] rounded-lg px-2.5 py-2 border ${"bg-[var(--t-panel)] border-[var(--t-line)]"}`}>
        <span className={`font-semibold ${"text-[var(--t-soft)]"}`}>Holding cost</span>
        <input type="number" min="0" max="200" value={holdingPct}
          onChange={(e) => setHoldingPct && setHoldingPct(e.target.value)}
          className={`w-16 rounded border px-1.5 py-0.5 text-[15px] ${"bg-[var(--t-panel)] border-[var(--t-line2)] text-[var(--t-ink)]"}`} />
        <span className={muted}>% / yr</span>
        <button onClick={(e) => { e.preventDefault(); setShowWizard((v) => !v); }}
          className={`ml-auto text-[14px] font-semibold ${"text-[var(--t-accent)]"} hover:underline`}>
          {showWizard ? "close" : "not sure?"}
        </button>
      </label>
      {showWizard && (
        <HoldingWizard lm={lm}
          onApply={(v) => { setHoldingPct && setHoldingPct(v); setShowWizard(false); }}
          onClose={() => setShowWizard(false)} />
      )}

      <a href={`${API}/api/template`} download className={`flex items-center justify-center gap-1.5 text-[15px] border rounded-lg py-2 transition-colors ${"text-[var(--t-accent)] hover:text-[var(--t-accent)] border-[var(--t-line2)] hover:border-[var(--t-accent-line)]"}`}>
        <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
        </svg>
        Download template
      </a>
      <p className={`text-[14px] ${"text-[var(--t-dim)]"} leading-relaxed`}>
        Required: <span className={`font-mono ${"text-[var(--t-soft)]"}`}>Date</span>, <span className={`font-mono ${"text-[var(--t-soft)]"}`}>Units_Sold</span>.{" "}
        Optional: <span className={`font-mono ${"text-[var(--t-soft)]"}`}>SKU</span>, <span className={`font-mono ${"text-[var(--t-soft)]"}`}>SKU_Name</span>,{" "}
        <span className={`font-mono ${"text-[var(--t-soft)]"}`}>Price</span>, <span className={`font-mono ${"text-[var(--t-soft)]"}`}>On_Promotion</span>,{" "}
        <span className={`font-mono ${"text-[var(--t-soft)]"}`}>Units_In_Stock</span>.
      </p>
      </>
      )}
    </div>
  );
}
