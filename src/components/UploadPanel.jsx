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
  const muted = "text-[var(--t-dim)]";
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
    ? "border-2 border-dashed border-[var(--t-line2)] rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group bg-[var(--t-sunken)]"
    : "border-2 border-dashed border-[var(--t-line2)] rounded-xl p-5 flex flex-col items-center gap-2 cursor-pointer hover:border-[var(--t-accent-line)] hover:bg-[var(--t-accent-soft)] transition-all group";
  const iconBox = lm
    ? "h-8 w-8 rounded-xl bg-[var(--t-panel)] border border-[var(--t-line)] group-hover:border-[var(--t-accent-line)] flex items-center justify-center transition-colors text-[var(--t-dim)] group-hover:text-[var(--t-accent)]"
    : "h-8 w-8 rounded-xl bg-[var(--t-line)] group-hover:bg-[var(--t-line)] flex items-center justify-center transition-colors text-[var(--t-dim)] group-hover:text-[var(--t-accent)]";

  const tabBtn = (m, label) => (
    <button onClick={() => setMode(m)}
      className={`flex-1 text-[14px] font-semibold py-1.5 rounded-md transition-colors ${mode === m
        ? ("bg-[var(--t-panel)] text-[var(--t-ink)] shadow-sm")
        : ("text-[var(--t-dim)] hover:text-[var(--t-soft)]")}`}>{label}</button>
  );
  const fld = lm
    ? "w-full bg-[var(--t-panel)] border border-[var(--t-line2)] rounded-lg px-2.5 py-2 text-[15px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]"
    : "w-full bg-[#161619] border border-[var(--t-line)] rounded-lg px-2.5 py-2 text-[15px] text-[var(--t-ink)] focus:outline-none focus:border-[var(--t-accent-line)]";

  return (
    <div className="space-y-2.5">
      <div className={`flex gap-1 p-0.5 rounded-lg ${"bg-[var(--t-sunken)]"}`}>
        {tabBtn("file", "Upload file")}{tabBtn("shopify", "Shopify")}
      </div>

      {mode === "shopify" ? (
        <div className="space-y-2">
          <input value={shop} onChange={e => setShop(e.target.value)} placeholder="store-name (before .myshopify.com)" className={fld} />
          <input value={token} onChange={e => setToken(e.target.value)} type="password" placeholder="Admin API token (shpat_…)" className={fld} />
          <div className="flex items-center gap-2">
            <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>History</span>
            <input value={days} onChange={e => setDays(e.target.value.replace(/[^0-9]/g, ""))} placeholder="all" className={`${fld} !w-24`} />
            <span className={`text-[14px] ${"text-[var(--t-dim)]"}`}>days — blank = all</span>
          </div>
          <button onClick={handleShopify} disabled={isUploading}
            className="w-full rounded-lg py-2 text-[15px] font-bold text-[var(--t-ink)] bg-[var(--t-accent-soft)] hover:bg-[var(--t-accent-soft)] disabled:opacity-60 transition-colors">
            {isUploading ? "Connecting…" : "Connect & import"}
          </button>
          <p className={`text-[14px] leading-relaxed ${"text-[var(--t-dim)]"}`}>
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
              ? "New products are added alongside what you have; products already loaded get this file's rows merged into their history by date. Everything is forecast and tested as one catalog, and the Backtest tab reports each file separately."
              : <span className={"text-[var(--t-bad)]"}>Off — this file will REPLACE everything currently loaded.</span>}
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