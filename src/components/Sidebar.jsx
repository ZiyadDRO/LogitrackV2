import React, { useState, useEffect, useRef } from 'react';
import { urgencyLevel, URGENCY_STYLES, URGENCY_STYLES_LM } from '../lib/helpers';

// ─── SKU LIST ITEM ────────────────────────────
export function SkuListItem({ sku, isActive, onClick, onDelete, reorderDays, hasOpenPO, indented = false, lm, secondaryAction, provisional, healthStatus }) {
  // Baseline/young SKUs are provisional — never show a hard urgency badge for them.
  const urgency = provisional ? "none" : urgencyLevel(reorderDays ?? null, hasOpenPO);
  const us = lm ? URGENCY_STYLES_LM[urgency] : URGENCY_STYLES[urgency];
  const activeClass = lm
    ? "bg-violet-50 border-violet-400 ring-1 ring-violet-300"
    : "bg-violet-950/40 border-violet-800/60 ring-1 ring-violet-700/30";
  const hoverClass = lm
    ? "border-transparent hover:bg-slate-100 hover:border-slate-300"
    : "border-transparent hover:bg-slate-800/50 hover:border-slate-700/50";
  const nameClass = lm ? "text-slate-900" : "text-white";
  const idClass   = lm ? "text-slate-500" : "text-slate-500";
  const canDelete = true;
  const hasActions = !!secondaryAction || canDelete;
  const iconBtn = lm
    ? "text-slate-400 hover:text-slate-700 hover:bg-slate-200"
    : "text-slate-400 hover:text-slate-200 hover:bg-slate-700/70";
  const badgeText = provisional
    ? "NEW"
    : healthStatus === "Dead stock"
    ? "DEAD"
    : healthStatus === "Overstocked"
    ? "SLOW"
    : urgency === "on_order"
    ? "ON ORDER"
    : (urgency !== "none" && reorderDays !== undefined)
      ? (reorderDays < 0 ? "OVERDUE" : reorderDays === 0 ? "TODAY" : `${reorderDays}d`)
      : null;
  return (
    <div onClick={onClick}
         className={`relative group flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer transition-all border ${
           indented ? "ml-4" : ""
         } ${isActive ? activeClass : hoverClass}`}>
      <div className={`h-2 w-2 rounded-full shrink-0 ${
        healthStatus === "Dead stock" ? "bg-slate-500"
        : healthStatus === "Overstocked" ? "bg-amber-400"
        : healthStatus === "Stockout risk" ? "bg-red-400 animate-pulse"
        : us.dot
      }`} />
      <div className="flex-1 min-w-0">
        <div className={`text-xs font-semibold ${nameClass} truncate`}>{sku.name}</div>
        <div className="flex items-center gap-1.5 mt-0.5">
          <div className={`text-[11px] ${idClass} font-mono truncate`}>{sku.id}</div>
        </div>
      </div>
      {/* Right side: urgency badge by default; on hover it yields to the action
          buttons (same slot), so nothing overlaps. */}
      <div className="shrink-0 flex items-center">
        {badgeText && (
          <span className={`text-[11px] font-mono font-bold ${hasActions ? "group-hover:hidden" : ""} ${
            healthStatus === "Dead stock" ? (lm ? "text-slate-500" : "text-slate-400")
            : healthStatus === "Overstocked" ? (lm ? "text-amber-600" : "text-amber-400")
            : provisional ? (lm ? "text-violet-500" : "text-violet-400")
            : urgency === "on_order" ? "text-violet-500" : us.text
          }`}>
            {badgeText}
          </span>
        )}
        {hasActions && (
          <div className={`items-center gap-1 ${badgeText ? "hidden group-hover:flex" : "flex opacity-0 group-hover:opacity-100"} transition-opacity`}>
            {secondaryAction && (
              <button onClick={e => { e.stopPropagation(); secondaryAction.onClick(); }} title={secondaryAction.title}
                className={`h-6 w-6 rounded-md flex items-center justify-center transition-all ${iconBtn}`}>
                <svg className="h-[15px] w-[15px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d={secondaryAction.iconPath} />
                </svg>
              </button>
            )}
            {canDelete && (
              <button onClick={e => { e.stopPropagation(); onDelete(sku.id); }} title="Delete SKU"
                className={`h-6 w-6 rounded-md flex items-center justify-center transition-all ${lm ? "text-slate-400 hover:text-red-600 hover:bg-red-100" : "text-slate-400 hover:text-red-300 hover:bg-red-900/50"}`}>
                <svg className="h-[15px] w-[15px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── FOLDER ROW ───────────────────────────────
export function FolderRow({ folderId, folder, allFolders = {}, skuList, skuForecasts, openPOs, activeSku, onSelectSku, onDeleteSku,
                     onRename, onDelete, onToggleCollapse, onRemoveSkuFromFolder, onAddSubfolder, lm, scoreBySku = {}, depth = 0 }) {
  const [editingName, setEditingName] = useState(false);
  const [nameVal, setNameVal]         = useState(folder.name);
  const [showMenu, setShowMenu]       = useState(false);
  const nameRef = useRef(null);
  useEffect(() => { if (editingName) nameRef.current?.focus(); }, [editingName]);
  const commitRename = () => {
    setEditingName(false);
    const trimmed = nameVal.trim();
    if (trimmed) onRename(folderId, trimmed);
    else setNameVal(folder.name);
  };
  const subfolders = Object.entries(allFolders).filter(([, f]) => f.parentId === folderId);
  const folderSkus = folder.skuIds
    .map(id => ({ sku: skuList.find(s => s.id === id), fc: skuForecasts.find(f => f.skuId === id) }))
    .filter(x => x.sku);
  // Count includes SKUs nested in subfolders so a parent reflects everything under it.
  const subSkuCount = subfolders.reduce((sum, [, f]) => sum + (f.skuIds?.filter(id => skuList.some(s => s.id === id)).length || 0), 0);
  const totalCount = folderSkus.length + subSkuCount;
  const urgencyOrder = { critical: 0, high: 1, medium: 2, low: 3, on_order: 4, none: 5 };
  const worstUrgency = folderSkus.reduce((worst, { fc }) => {
    const u = urgencyLevel(fc?.daysUntilReorder ?? null, !!openPOs[fc?.skuId]);
    return (urgencyOrder[u] ?? 5) < (urgencyOrder[worst] ?? 5) ? u : worst;
  }, "none");
  const us = lm ? URGENCY_STYLES_LM[worstUrgency] : URGENCY_STYLES[worstUrgency];
  const folderDot = us?.dot ?? (lm ? "bg-slate-300" : "bg-slate-700");
  const hoverRow = lm ? "hover:bg-slate-100" : "hover:bg-slate-800/40";
  const nameText = lm ? "text-slate-800" : "text-slate-300";
  const iconText = lm ? "text-slate-400" : "text-slate-500";
  const menuBg   = lm ? "bg-white border-slate-200 shadow-lg" : "bg-[#161619] border-slate-700 shadow-xl";
  const menuItem = lm ? "text-slate-700 hover:bg-slate-100" : "text-slate-300 hover:bg-slate-800";
  return (
    <div>
      <div className={`group flex items-center gap-1.5 px-2 py-1.5 rounded-xl ${hoverRow} transition-all relative`}>
        <button onClick={() => onToggleCollapse(folderId)}
          className={`h-4 w-4 flex items-center justify-center ${iconText} hover:text-slate-500 transition-colors shrink-0`}>
          <svg className={`h-2.5 w-2.5 transition-transform ${folder.collapsed ? "" : "rotate-90"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M9 5l7 7-7 7" />
          </svg>
        </button>
        <div className="relative shrink-0">
          <svg className={`h-5 w-5 ${iconText}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
          </svg>
          {worstUrgency !== "none" && <div className={`absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full ${folderDot}`} />}
        </div>
        {editingName ? (
          <input ref={nameRef} value={nameVal}
            onChange={e => setNameVal(e.target.value)}
            onBlur={commitRename}
            onKeyDown={e => { if (e.key === "Enter") commitRename(); if (e.key === "Escape") { setEditingName(false); setNameVal(folder.name); } }}
            className={`flex-1 min-w-0 rounded px-1.5 py-0.5 text-xs focus:outline-none ${lm ? "bg-white border border-violet-400 text-slate-900" : "bg-slate-800 border border-violet-700/60 text-white"}`}
          />
        ) : (
          <span className={`flex-1 min-w-0 text-xs font-semibold ${nameText} truncate`} onDoubleClick={() => setEditingName(true)}>
            {folder.name}
          </span>
        )}
        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded-md shrink-0 ${lm ? "bg-slate-200 text-slate-500" : "bg-slate-800/60 text-slate-500"}`}>
          {totalCount}
        </span>
        <button onClick={e => { e.stopPropagation(); setShowMenu(v => !v); }}
          className={`opacity-0 group-hover:opacity-100 h-5 w-5 rounded flex items-center justify-center transition-all shrink-0 ${lm ? "text-slate-400 hover:text-slate-600 hover:bg-slate-200" : "text-slate-600 hover:text-slate-300 hover:bg-slate-700"}`}>
          <svg className="h-3 w-3" fill="currentColor" viewBox="0 0 24 24">
            <circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" />
          </svg>
        </button>
        {showMenu && (
          <div className={`absolute right-1 top-7 z-30 ${menuBg} border rounded-xl py-1 min-w-[140px]`} onMouseLeave={() => setShowMenu(false)}>
            <button onClick={() => { setEditingName(true); setShowMenu(false); }}
              className={`w-full text-left px-3 py-1.5 text-xs ${menuItem} flex items-center gap-2`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
              Rename
            </button>
            {depth === 0 && onAddSubfolder && (
              <button onClick={() => { onAddSubfolder(folderId); setShowMenu(false); }}
                className={`w-full text-left px-3 py-1.5 text-xs ${menuItem} flex items-center gap-2`}>
                <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 13h6m-3-3v6m-9 1V7a2 2 0 012-2h6l2 2h6a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
                </svg>
                Add subfolder
              </button>
            )}
            <button onClick={() => { onDelete(folderId); setShowMenu(false); }}
              className={`w-full text-left px-3 py-1.5 text-xs text-rose-600 hover:bg-rose-50 flex items-center gap-2`}>
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              Delete folder
            </button>
          </div>
        )}
      </div>
      {!folder.collapsed && (
        <div className={`space-y-0.5 ${depth === 0 ? "ml-3 pl-1 border-l " + (lm ? "border-slate-200" : "border-slate-800") : ""}`}>
          {subfolders.map(([subId, subFolder]) => (
            <FolderRow key={subId} folderId={subId} folder={subFolder} allFolders={allFolders} skuList={skuList} skuForecasts={skuForecasts}
              openPOs={openPOs} activeSku={activeSku} onSelectSku={onSelectSku} onDeleteSku={onDeleteSku}
              onRename={onRename} onDelete={onDelete} onToggleCollapse={onToggleCollapse}
              onRemoveSkuFromFolder={onRemoveSkuFromFolder} onAddSubfolder={onAddSubfolder} lm={lm} scoreBySku={scoreBySku} depth={depth + 1} />
          ))}
          {folderSkus.map(({ sku, fc }) => (
            <SkuListItem key={sku.id} sku={sku} isActive={activeSku === sku.id} onClick={() => onSelectSku(sku.id)}
              onDelete={onDeleteSku} reorderDays={fc?.daysUntilReorder} hasOpenPO={!!openPOs[sku.id]} indented lm={lm}
              healthStatus={scoreBySku[sku.id]?.status}
              provisional={fc?.tooNew || fc?.young}
              secondaryAction={{ title: "Remove from folder", onClick: () => onRemoveSkuFromFolder(folderId, sku.id), iconPath: "M11 7l-4 4m0 0l4 4m-4-4h14M3 5v14" }} />
          ))}
          {folderSkus.length === 0 && subfolders.length === 0 && (
            <div className={`ml-4 px-3 py-2 text-[11px] italic ${lm ? "text-slate-400" : "text-slate-700"}`}>Empty folder</div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── FLEET ALERT BANNER ───────────────────────
export function FleetAlertBanner({ skuForecasts, openPOs, lm, leadTimeOf = null }) {
  /* Per-SKU bands, so a 60-day sea line isn't judged by a 14-day line's clock.
     leadTimeOf is optional — without it every product falls back to the default
     lead time, which is what this banner did for all of them before. */
  const lt = s => (leadTimeOf ? leadTimeOf(s.skuId) : null);
  const critical = skuForecasts.filter(s => !openPOs[s.skuId] && urgencyLevel(s.daysUntilReorder, false, lt(s)) === "critical");
  const urgent   = skuForecasts.filter(s => !openPOs[s.skuId] && urgencyLevel(s.daysUntilReorder, false, lt(s)) === "high");
  if (critical.length === 0 && urgent.length === 0) return null;
  const isCrit = critical.length > 0;
  const bg   = lm ? (isCrit ? "bg-red-50 border-red-300" : "bg-amber-50 border-amber-300") : (isCrit ? "bg-red-950/25 border-red-900/40" : "bg-amber-950/20 border-amber-900/30");
  const text = lm ? (isCrit ? "text-red-700" : "text-amber-700") : (isCrit ? "text-red-300" : "text-amber-300");
  const sub  = lm ? "text-slate-600" : "text-slate-400";
  return (
    <div className={`rounded-2xl p-4 border flex gap-3 items-start ${bg}`}>
      <span className="text-base mt-0.5">{isCrit ? "🚨" : "⚠️"}</span>
      <div className="space-y-0.5">
        <div className={`text-sm font-bold ${text}`}>
          {/* No day count in the copy. The window is a fraction of each product's own
              lead time now, so any single number here would be wrong for most of the
              products the line is counting — which is exactly how this said "within 7
              days" while the filter above it used a different figure. */}
          {isCrit ? `${critical.length} SKU${critical.length > 1 ? "s" : ""} AT STOCKOUT RISK`
                  : `${urgent.length} SKU${urgent.length > 1 ? "s" : ""} due to reorder`}
        </div>
        <div className={`text-xs ${sub} flex flex-wrap gap-x-3 gap-y-1`}>
          {[...critical, ...urgent].map(s => (
            <span key={s.skuId} className="font-mono">
              {s.skuName}{" "}
              {s.daysUntilReorder <= 0
                ? <span className={lm ? "text-red-600" : "text-red-400"}>— {Math.abs(s.daysUntilReorder)}d overdue</span>
                : <span className={lm ? "text-amber-600" : "text-amber-400"}>— {s.daysUntilReorder}d left</span>}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}