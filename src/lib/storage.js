// Versioned localStorage helpers. Values are stored as {__v, data} so future
// schema changes can migrate instead of crashing on old data. Reading a legacy
// unversioned value (pre-split builds) still works — it's returned as-is.
const VERSION = 1;

export function loadStorage(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.__v !== undefined) {
      return parsed.data ?? fallback;
    }
    return parsed; // legacy unversioned value
  } catch {
    return fallback;
  }
}

export function saveStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify({ __v: VERSION, data: value }));
  } catch {}
}
