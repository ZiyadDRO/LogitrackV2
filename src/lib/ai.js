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
// NOTE: meta-llama/llama-4-scout-17b-16e-instruct was deprecated by Groq on
// 2026-06-17 for free/developer tiers and now 404s with "model does not exist".
// llama-3.3-70b-versatile is a current Groq *production* model (131k context,
// 32k max completion) and matches the "Llama 3.3 70B" label shown in AiDrawer.
// Check https://console.groq.com/docs/deprecations before changing this again.
export const GROQ_MODEL = "llama-3.3-70b-versatile";
