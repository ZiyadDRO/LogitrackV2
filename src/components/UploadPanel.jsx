import React, { useState, useRef } from 'react';
import HoldingWizard from './HoldingWizard';
import { API } from '../lib/api';

// ─── UPLOAD PANEL ─────────────────────────────

export default function UploadPanel({ onUploadSuccess, onUploadError, isUploading, setIsUploading, lm,
                                     holdingPct = 25, setHoldingPct = null }) {
  const fileRef = useRef(null);
  const [mode, setMode] = useState("file");          // file | shopify
  const [shop, setShop] = useState("");
  const [token, setToken] = useState("");
  const [days, setDays] = useState("");   // blank = pull all available history
  // ON by default. A second upload used to silently wipe the first, which is never what
  // you want when you're building up a catalog file by file. Everything is modelled as
  // one catalog; the backtest still reports each file's products separately.
  const [addToExisting, setAddToExisting] = useState(true);
  const muted = lm ? "text-slate-500" : "text-slate-500";
  const [showWizard, setShowWizard] = useState(false);
  const handleShopify = async () => {
    if (!shop.trim() || !token.trim()) { onUploadError("Enter your Shopify store name and Admin API token."); return; }
    setIsUploading(true); onUploadError(null);
    try {
      const res = await fetch(`${API}/api/upload/shopify`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ shop: shop.trim(), token: token.trim(), days: days.trim() ? Number(days) : null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Shopify import failed.");
      onUploadSuccess(data);
    } catch (err) {
      onUploadError(err.message || "Network error — is the server running?");
    } finally { setIsUploading(false); }
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
      onUploadError(err.message || "Network error - is the server running?");
    } finally {
      setIsUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const dropzone = lm
    ? "border-2 border-dashed border-slate-300 rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-violet-400 hover:bg-violet-50 transition-all group bg-slate-50"
    : "border-2 border-dashed border-slate-700/80 rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-violet-600/60 hover:bg-violet-950/10 transition-all group";
  const iconBox = lm
    ? "h-8 w-8 rounded-xl bg-white border border-slate-200 group-hover:border-violet-300 flex items-center justify-center transition-colors text-slate-400 group-hover:text-violet-600"
    : "h-8 w-8 rounded-xl bg-slate-800 group-hover:bg-slate-700 flex items-center justify-center transition-colors text-slate-400 group-hover:text-violet-400";

  const tabBtn = (m, label) => (
    <button onClick={() => setMode(m)}
      className={`flex-1 text-[11px] font-semibold py-1.5 rounded-md transition-colors ${mode === m
        ? (lm ? "bg-white text-slate-900 shadow-sm" : "bg-slate-700 text-white")
        : (lm ? "text-slate-500 hover:text-slate-700" : "text-slate-400 hover:text-slate-200")}`}>{label}</button>
  );
  const fld = lm
    ? "w-full bg-white border border-slate-300 rounded-lg px-2.5 py-2 text-xs text-slate-900 focus:outline-none focus:border-violet-500"
    : "w-full bg-[#161619] border border-white/10 rounded-lg px-2.5 py-2 text-xs text-slate-200 focus:outline-none focus:border-violet-500/60";

  return (
    <div className="space-y-2.5">
      <div className={`flex gap-1 p-0.5 rounded-lg ${lm ? "bg-slate-100" : "bg-white/5"}`}>
        {tabBtn("file", "Upload file")}{tabBtn("shopify", "Shopify")}
      </div>

      {mode === "shopify" ? (
        <div className="space-y-2">
          <input value={shop} onChange={e => setShop(e.target.value)} placeholder="store-name (before .myshopify.com)" className={fld} />
          <input value={token} onChange={e => setToken(e.target.value)} type="password" placeholder="Admin API token (shpat_…)" className={fld} />
          <div className="flex items-center gap-2">
            <span className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-500"}`}>History</span>
            <input value={days} onChange={e => setDays(e.target.value.replace(/[^0-9]/g, ""))} placeholder="all" className={`${fld} !w-24`} />
            <span className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-500"}`}>days — blank = all</span>
          </div>
          <button onClick={handleShopify} disabled={isUploading}
            className="w-full rounded-lg py-2 text-xs font-bold text-white bg-violet-600 hover:bg-violet-500 disabled:opacity-60 transition-colors">
            {isUploading ? "Connecting…" : "Connect & import"}
          </button>
          <p className={`text-[11px] leading-relaxed ${lm ? "text-slate-500" : "text-slate-600"}`}>
            Use a <span className="font-semibold">read-only</span> Admin API token (scope <span className="font-mono">read_orders</span>). Shopify only returns the last <span className="font-semibold">60 days</span> of orders unless your app has <span className="font-mono">read_all_orders</span> approved — needed for a meaningful backtest.
          </p>
        </div>
      ) : (
      <>
      <div onDrop={e => { e.preventDefault(); handleFile(e.dataTransfer.files[0]); }} onDragOver={e => e.preventDefault()} onClick={() => fileRef.current?.click()} className={dropzone}>
        <div className={iconBox}>
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
          </svg>
        </div>
        <span className={`text-xs font-semibold text-center ${lm ? "text-slate-700" : "text-slate-300"}`}>{isUploading ? "Uploading..." : "Drop Excel file or click to browse"}</span>
        <span className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-600"}`}>.xlsx or .xls · multi-SKU supported</span>
        <input ref={fileRef} type="file" accept=".xlsx,.xls,.csv" className="hidden" onChange={e => handleFile(e.target.files[0])} />
      </div>
      <label onClick={(e) => e.stopPropagation()}
        className={`flex items-start gap-2 text-[11px] cursor-pointer rounded-lg px-2.5 py-2 border transition-colors ${addToExisting
          ? (lm ? "bg-violet-50 border-violet-200" : "bg-violet-950/20 border-violet-900/40")
          : (lm ? "bg-white border-slate-200 hover:border-slate-300" : "bg-slate-900/40 border-slate-800 hover:border-slate-700")}`}>
        <input type="checkbox" checked={addToExisting} onChange={(e) => setAddToExisting(e.target.checked)} className="mt-0.5" />
        <span>
          <span className={`font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>Add to what&apos;s already loaded</span>
          <span className={`block ${lm ? "text-slate-500" : "text-slate-500"}`}>
            {addToExisting
              ? "New products are added alongside what you have; products already loaded get this file's rows merged into their history by date. Everything is forecast and tested as one catalog, and the Backtest tab reports each file separately."
              : <span className={lm ? "text-rose-600" : "text-rose-400"}>Off — this file will REPLACE everything currently loaded.</span>}
          </span>
        </span>
      </label>
      {/* Holding rate belongs HERE, not only in the Backtest tab: a backtest starts the
          moment you upload, so a client who knows their real figure must be able to set
          it beforehand rather than getting a run on 25% and needing another. */}
      <label onClick={(e) => e.stopPropagation()}
        className={`flex items-center gap-2 text-[11px] rounded-lg px-2.5 py-2 border ${lm ? "bg-white border-slate-200" : "bg-slate-900/40 border-slate-800"}`}>
        <span className={`font-semibold ${lm ? "text-slate-700" : "text-slate-300"}`}>Holding cost</span>
        <input type="number" min="0" max="200" value={holdingPct}
          onChange={(e) => setHoldingPct && setHoldingPct(e.target.value)}
          className={`w-16 rounded border px-1.5 py-0.5 text-xs ${lm ? "bg-white border-slate-300 text-slate-900" : "bg-slate-900 border-slate-700 text-slate-200"}`} />
        <span className={muted}>% / yr</span>
        <button onClick={(e) => { e.preventDefault(); setShowWizard((v) => !v); }}
          className={`ml-auto text-[11px] font-semibold ${lm ? "text-violet-600" : "text-violet-400"} hover:underline`}>
          {showWizard ? "close" : "not sure?"}
        </button>
      </label>
      {showWizard && (
        <HoldingWizard lm={lm}
          onApply={(v) => { setHoldingPct && setHoldingPct(v); setShowWizard(false); }}
          onClose={() => setShowWizard(false)} />
      )}

      <a href={`${API}/api/template`} download className={`flex items-center justify-center gap-1.5 text-xs border rounded-lg py-2 transition-colors ${lm ? "text-violet-600 hover:text-violet-700 border-slate-300 hover:border-violet-400" : "text-violet-400 hover:text-violet-300 border-white/10 hover:border-slate-700"}`}>
        <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
        </svg>
        Download template
      </a>
      <p className={`text-[11px] ${lm ? "text-slate-500" : "text-slate-600"} leading-relaxed`}>
        Required: <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>Date</span>, <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>Units_Sold</span>.{" "}
        Optional: <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>SKU</span>, <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>SKU_Name</span>,{" "}
        <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>Price</span>, <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>On_Promotion</span>,{" "}
        <span className={`font-mono ${lm ? "text-slate-700" : "text-slate-400"}`}>Units_In_Stock</span>.
      </p>
      </>
      )}
    </div>
  );
}