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
export const GROQ_NO_TOOLS = { compound_custom: { tools: { enabled_tools: [] } } };

/** Names any built-in tool a compound reply actually ran, or null. See GROQ_MODEL_CHAT. */
export function groqToolsRan(data) {
  const t = data?.choices?.[0]?.message?.executed_tools;
  if (!Array.isArray(t) || t.length === 0) return null;
  return t.map(x => x?.type || x?.name || "unknown").join(", ");
}
