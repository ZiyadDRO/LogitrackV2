// Single source of truth for the Groq client config.
//
// The key is NOT in this file any more, and must never come back. It used to be
// hardcoded here, and GitHub's push protection refused the commit that carried it
// (GH013 — "Groq API Key" found at src/lib/ai.js:7). Push protection was right:
// a key written here is compromised twice over. It goes into git history, and it
// also ships to every browser that loads the app and gets baked into dist/, so
// even a private repo doesn't keep it secret from anyone using the tool.
//
// Set it instead in a .env.local file at the project root — already git-ignored:
//
//     VITE_GROQ_API_KEY=gsk_your_key_here
//
// Vite only exposes variables prefixed with VITE_. After changing it, restart
// `npm run dev`, or re-run `npm run build` for the packaged app.
//
// With no key set this is an empty string and every AI feature degrades quietly:
// CategorizePanel returns early, BacktestTab reports "no AI key configured",
// and the rest of the tool — forecasting, scorecard, suppliers — is unaffected,
// because none of it touches Groq.
//
// The real fix is to stop shipping the key to the browser at all: proxy these
// calls through the FastAPI backend (POST /api/ai/complete) with the key in a
// server-side env var. Then it never reaches a client.
export const GROQ_API_KEY = import.meta.env?.VITE_GROQ_API_KEY ?? "";

export const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
// Groq retires models on a published schedule, and this one has now been bitten
// twice. The history, so the next person can see the pattern rather than the
// symptom:
//   meta-llama/llama-4-scout-17b-16e-instruct  shut down 2026-07-17
//   llama-3.3-70b-versatile                    shut down 2026-08-16
// Both began failing with "model does not exist", which reads like a broken key
// or a bad request rather than a retired model.
//
// openai/gpt-oss-120b is a current Groq PRODUCTION model with the same 131k
// context, and is the replacement Groq itself names for llama-3.3-70b-versatile.
// Preview models (qwen3.6-27b and friends) are explicitly not for production use,
// so they are not an option here however tempting the benchmarks look.
//
// Check https://console.groq.com/docs/deprecations before changing this again.
export const GROQ_MODEL = "openai/gpt-oss-120b";

// ── The fleet chat needs a different model, for THROUGHPUT rather than quality ──
//
// Picking gpt-oss-120b above, I checked context length and production status and not
// rate limits. On Groq's free tier it allows 8,000 tokens per MINUTE and 200,000 per
// DAY. The Ask AI drawer sends the whole catalogue as a system prompt on every message,
// which for 32 products is roughly 7,000-11,000 tokens, so a single question could
// exhaust the minute's allowance and the day capped out at about twenty questions.
//
// groq/compound-mini allows 70,000 tokens per minute with no daily token cap, carries
// the same 131k context, and is powered by Llama 3.3 70B and GPT-OSS 120B, so answer
// quality should not regress. Its cost is that it is an agentic SYSTEM: left alone it
// decides for itself when to run a web search, visit a site, execute code, or call
// Wolfram Alpha. For a tool reasoning over a client's private sales data an unprompted
// web search is both noise and an injection surface, since fetched page content would
// land in the same context as the catalogue.
//
// So tools are switched off explicitly. Groq documents `enabled_tools` as a way to
// narrow the set; it does NOT document what an empty array does, so the drawer also
// checks `message.executed_tools` on every reply and says so loudly if anything ran.
// Treat a warning there as "this is not safe for client data yet", not a cosmetic bug.
//
// The small structured calls (the natural-language event parser) stay on GROQ_MODEL:
// they are a few hundred tokens, have no throughput problem, and strict JSON extraction
// is not something to hand to an agent that might decide to search the web mid-parse.
export const GROQ_MODEL_CHAT = "groq/compound-mini";

// Spread into the request body for GROQ_MODEL_CHAT. Harmless for a plain model, which
// ignores unknown fields, so it does not need removing if the model changes back.
/** Spread into every request that uses GROQ_MODEL (a gpt-oss reasoning model).
 *
 * Without this the model spends its whole `max_tokens` budget on internal reasoning and
 * emits almost nothing. Measured against the live classification call — same model, same
 * prompt, same max_tokens of 2400:
 *
 *     without  ->  finish_reason "length",   135 chars, JSON.parse fails
 *     with     ->  finish_reason "stop",   3,989 chars, parses
 *
 * That failure was silent in the UI: the Grouping tab showed "Couldn't parse the AI
 * response. Try again or simplify the info.", which reads like a bad prompt rather than a
 * truncated one. Every other GROQ_MODEL caller reserves far LESS than 2400 (down to 12 in
 * classifyIndustry), so they were failing the same way with even less room.
 *
 * Not applied to GROQ_MODEL_CHAT (groq/compound-mini) — different family, different
 * parameter surface, and its callers stream prose rather than parse JSON.
 */
export const GROQ_LOW_REASONING = { reasoning_effort: "low" };

export const GROQ_NO_TOOLS = { compound_custom: { tools: { enabled_tools: [] } } };

/** Names any built-in tool a compound reply actually ran, or null. See GROQ_MODEL_CHAT. */
export function groqToolsRan(data) {
  const t = data?.choices?.[0]?.message?.executed_tools;
  if (!Array.isArray(t) || t.length === 0) return null;
  return t.map(x => x?.type || x?.name || "unknown").join(", ");
}


// ── Is the configured model still alive? ──────────────────────────────────────
//
// Groq retires models on a schedule and this project has been bitten twice:
// meta-llama/llama-4-scout shut down 2026-07-17, llama-3.3-70b-versatile 2026-08-16.
// Both times every AI feature broke at once, and the first thing anyone SAW was the
// event parser saying "Couldn't parse that. Try being more specific" — a message
// blaming the user's phrasing for a dead model. Six features failing separately with
// six generic errors is what a missing startup check looks like.
//
// So: ask Groq once, on load, which models this key can actually reach, and compare.
// One request per session, cached. It never blocks the UI — a failed check reports
// "unknown" rather than claiming a problem, because a flaky network is not a
// deprecation and crying wolf about it would be its own bug.
const GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models";
let _healthPromise = null;

/**
 * Resolves to { ok, checked, available, missing[], error }.
 *   ok      — every configured model is reachable (or the check couldn't run)
 *   checked — false when there's no key, or the request failed
 *   missing — configured model ids Groq did not list. THIS is the deprecation signal.
 */
export function checkGroqModels({ force = false } = {}) {
  if (_healthPromise && !force) return _healthPromise;
  _healthPromise = (async () => {
    if (!GROQ_API_KEY) {
      return { ok: true, checked: false, available: [], missing: [], error: "no API key set" };
    }
    try {
      const res = await fetch(GROQ_MODELS_URL, { headers: { Authorization: `Bearer ${GROQ_API_KEY}` } });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        return { ok: true, checked: false, available: [], missing: [],
                 error: body?.error?.message || `models endpoint returned ${res.status}` };
      }
      const data = await res.json();
      const available = (data?.data || []).map(m => m?.id).filter(Boolean);
      // A compound SYSTEM may not be listed the way a plain model is, so treat an id
      // we can't find as missing only when the listing looks complete enough to trust.
      const want = [GROQ_MODEL, GROQ_MODEL_CHAT];
      const missing = available.length ? want.filter(m => !available.includes(m)) : [];
      return { ok: missing.length === 0, checked: available.length > 0, available, missing, error: null };
    } catch (e) {
      return { ok: true, checked: false, available: [], missing: [], error: e?.message || "network error" };
    }
  })();
  return _healthPromise;
}

/** One line a human can act on, or null when there is nothing to say. */
export function groqHealthMessage(h) {
  if (!h || h.ok) return null;
  const list = h.missing.join(" and ");
  return `Groq no longer offers ${list}. AI features will fail until the model is changed `
       + `in src/lib/ai.js — check https://console.groq.com/docs/deprecations for the replacement.`;
}

// ── What did that request actually cost? ──────────────────────────────────────
//
// Every size limit hit in this project was diagnosed by guessing at token counts from
// character lengths, and the guesses were wrong twice — once by 2.4x on the per-product
// block, once by 1.8x on a test payload. Groq returns the real number in `usage`. Record
// it, so the next person sizing a prompt reads a measurement instead of estimating.
//
// The ceiling is not the advertised per-minute limit. groq/compound-mini routes to
// sub-models (openai/gpt-oss-120b among them) that carry their own free-tier caps, so
// requests above roughly 6,400 measured tokens come back 429 naming the SUB-model while
// compound still shows most of its 70,000 unused.
export const GROQ_REQUEST_CEILING = 6400;
let _lastUsage = null;

/**
 * @param data     the parsed Groq response
 * @param reserved the max_tokens the request asked for
 *
 * `reserved` matters: Groq bills the rate limiter for prompt PLUS the completion you
 * reserved, not for what the reply actually used. Its own refusal spelled this out —
 * "Requested 10935" on a payload whose prompt was ~10,200 with max_tokens 700. Judging
 * headroom on prompt_tokens alone therefore reads ~700 tokens rosier than reality, and
 * an instrument that flatters itself is worse than no instrument.
 */
export function recordGroqUsage(data, reserved = 0) {
  const u = data?.usage;
  if (!u) return null;
  _lastUsage = {
    prompt: u.prompt_tokens ?? null,
    completion: u.completion_tokens ?? null,
    total: u.total_tokens ?? null,
    reserved,
    at: Date.now(),
  };
  return _lastUsage;
}

export function lastGroqUsage() { return _lastUsage; }

/** "5,221 / 6,400 tokens" plus a flag when the margin is thin. */
export function usageSummary() {
  if (!_lastUsage?.prompt) return null;
  // What the limiter counted, not what the reply used.
  const charged = _lastUsage.prompt + (_lastUsage.reserved || 0);
  return {
    prompt: _lastUsage.prompt,
    charged,
    ceiling: GROQ_REQUEST_CEILING,
    pctUsed: Math.round((charged / GROQ_REQUEST_CEILING) * 100),
    tight: charged > GROQ_REQUEST_CEILING * 0.85,
    text: `${charged.toLocaleString()} / ${GROQ_REQUEST_CEILING.toLocaleString()} tokens`,
  };
}
