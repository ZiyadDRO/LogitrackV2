// Backend API base + a shared fetch helper that SURFACES failures instead of
// swallowing them. Every backend call used to be `.catch(() => null)` — a failing
// SKU just silently vanished from the dashboard. fetchJson keeps the same
// "null on failure" contract (callers already handle null) but reports the error
// to the toast stack so the user can see something went wrong.

export const API_HOST = window.location.hostname || "127.0.0.1";
// Always talk to the backend over http(s). If the page was opened directly from disk
// (file://), window.location.protocol would be "file:" — fall back to http.
export const API_PROTO = window.location.protocol === "https:" ? "https:" : "http:";
export const API = `${API_PROTO}//${API_HOST}:8000`;
// Report the browser's timezone so the backend's "today" matches the user's local date.
export const TZ = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone || "");

/**
 * The ONE way the app asks for a product's forecast.
 *
 * There used to be three hand-built copies of this URL (the Fleet's page load, the Fleet's
 * per-product refresh, the product page) and each sent a different subset of the settings.
 * The product page left out where the stock figure came from, so a count typed on a store
 * with no stock column read as "not counted" there (no reorder date, order 0) while the
 * Fleet showed the same product overdue. The page load left out cost, fees and the chosen
 * protection level, so its order quantities differed from the product page until
 * something else refreshed them. Every caller now goes through here, so every view asks
 * the same question.
 *
 * `p`         the product's settings (DEFAULT_PARAMS merged in by the caller)
 * `leadTime`  the PLANNED lead time (planningLeadTime(...).days), never p.leadTime: the
 *             typed field is only one input to it
 */
export function forecastUrl(skuId, p, { leadTime, months = 3, onOrderQty = 0, onOrderEta = null,
                                        strategy = "balanced" } = {}) {
  const has = (v) => v != null && v !== "";
  const q = [
    `sku_id=${encodeURIComponent(skuId)}`,
    `stock=${p.stock}`,
    `lead_time_days=${leadTime ?? p.leadTime}`,
    `coverage_days=${p.coverage}`,
    `strategy=${strategy}`,
    `forecast_months=${months}`,
    `units_on_order=${onOrderQty || 0}`,
    onOrderEta != null ? `on_order_eta_days=${onOrderEta}` : null,
    has(p.unitCost) ? `unit_cost=${p.unitCost}` : null,
    has(p.fees) ? `fees=${p.fees}` : null,
    p.protection ? `protection=${p.protection}` : null,
    `stock_source=${encodeURIComponent(p.stockSource || "unknown")}`,
    p.stockCountedAt ? `stock_counted_at=${encodeURIComponent(p.stockCountedAt)}` : null,
    `tz=${TZ}`,
  ].filter(Boolean);
  return `${API}/api/forecast?${q.join("&")}`;
}

// ── error toast plumbing ──────────────────────────────────────────────────────
const listeners = new Set();
let nextId = 1;
const recent = new Map(); // message → timestamp, to dedupe bursts

export function onApiError(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function reportApiError(message) {
  const now = Date.now();
  const last = recent.get(message);
  if (last && now - last < 5000) return; // dedupe identical errors within 5s
  recent.set(message, now);
  for (const [msg, ts] of recent) if (now - ts > 30000) recent.delete(msg);
  const evt = { id: nextId++, message };
  listeners.forEach(cb => { try { cb(evt); } catch {} });
}

// ── fetch helper ──────────────────────────────────────────────────────────────
// Returns parsed JSON on success, null on any failure (after reporting a toast).
/**
 * Turn an error body into something a human can act on.
 *
 * FastAPI's `detail` is a plain string for HTTPException but an ARRAY of validation
 * objects for a 422. Interpolating that array straight into a template literal renders
 * "[object Object]" — which is how a real bug (a lead time of 0 being rejected) reached
 * the screen as an unreadable blob and cost an afternoon to trace.
 */
export function describeApiError(data, status) {
  const d = data?.detail;
  if (typeof d === "string" && d.trim()) return d;
  if (Array.isArray(d) && d.length) {
    return d.map((x) => {
      if (typeof x === "string") return x;
      // loc is like ["query", "lead_time_days"] — the last part is the field at fault.
      const field = Array.isArray(x?.loc) ? x.loc[x.loc.length - 1] : null;
      const msg = x?.msg || x?.type || "invalid value";
      return field ? `${field}: ${msg}` : msg;
    }).join("; ");
  }
  if (d && typeof d === "object") {
    try { return JSON.stringify(d); } catch { /* fall through */ }
  }
  if (typeof data?.message === "string") return data.message;
  return `HTTP ${status}`;
}

export async function fetchJson(url, opts = undefined, label = "Request") {
  try {
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      reportApiError(`${label} failed: ${describeApiError(data, res.status)}`);
      return null;
    }
    return data;
  } catch {
    reportApiError(`${label} failed. Is the backend running?`);
    return null;
  }
}
