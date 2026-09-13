#!/usr/bin/env bash
# Why is the Ask AI drawer failing? Sends the exact request shapes the app uses.
#
# Uses curl, not Python urllib: Cloudflare sits in front of api.groq.com and blocks
# urllib's default User-Agent with "error code: 1010", which looks like an account
# problem and is not one.
#
# Your key is read from .env.local and never printed.
set -uo pipefail
cd "$(dirname "$0")"
KEY=$(grep -E '^VITE_GROQ_API_KEY=' .env.local | cut -d= -f2- | tr -d '"'"'"' \r')
[ -n "${KEY:-}" ] || { echo "No VITE_GROQ_API_KEY in .env.local"; exit 1; }
URL=https://api.groq.com/openai/v1/chat/completions
T=$(mktemp -d)

python3 - "$T" <<'PY'
import json, sys
T = sys.argv[1]
MINI, PLAIN = "groq/compound-mini", "openai/gpt-oss-120b"
NO = {"compound_custom": {"tools": {"enabled_tools": []}}}
tiny = [{"role":"user","content":"Reply with the single word: ok"}]
big  = [{"role":"system","content":"You are an inventory analyst.\n" + "Product row with status, cover, order qty and volatility. "*900},
        {"role":"user","content":"Which products need reordering first?"}]
for name, body in [
    ("1", {"model":MINI, "messages":tiny, "max_tokens":20}),
    ("2", {"model":MINI, **NO, "messages":tiny, "max_tokens":20}),
    ("3", {"model":MINI, **NO, "messages":big, "max_tokens":700}),
    ("4", {"model":PLAIN, "messages":big, "max_tokens":700}),
    ("5", {"model":MINI, "messages":[{"role":"user","content":"Search the web for today's top news headline."}], **NO, "max_tokens":150}),
]:
    json.dump(body, open(f"{T}/{name}.json","w"))
PY

run () {
  echo; echo "--- $2"
  CODE=$(curl -sS -o "$T/out.$1" -D "$T/hdr.$1" -w '%{http_code}' --max-time 90 "$URL" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" \
    --data @"$T/$1.json" 2>"$T/err.$1")
  echo "    HTTP $CODE"
  [ -s "$T/err.$1" ] && echo "    curl: $(head -c 200 "$T/err.$1")"
  python3 - "$T/out.$1" <<'PY'
import json,sys
raw=open(sys.argv[1]).read()
try:
    d=json.loads(raw)
except Exception:
    print("    non-JSON body:", raw[:200].replace("\n"," ")); raise SystemExit
if "error" in d:
    print("    error:", str(d["error"].get("message"))[:300])
else:
    m=(d.get("choices") or [{}])[0].get("message",{})
    u=d.get("usage") or {}
    print("    OK  executed_tools:", m.get("executed_tools") or "none")
    print("    reply:", (m.get("content") or "")[:100].strip().replace("\n"," "))
    print("    prompt tokens:", u.get("prompt_tokens"), " completion:", u.get("completion_tokens"))
PY
  grep -iE '^(retry-after|x-ratelimit-(remaining|limit|reset)-tokens)' "$T/hdr.$1" | sed 's/^/    /'
}

echo "Sending the exact request shapes the Ask AI drawer uses (key never printed)."
run 1 "compound-mini, tiny prompt, NO compound_custom"
run 2 "compound-mini, tiny prompt, WITH compound_custom (what the app sends)"
run 3 "compound-mini, ~6k-token prompt, WITH compound_custom (real size)"
run 4 "plain gpt-oss-120b, ~6k-token prompt (the fallback)"
run 5 "compound-mini, a prompt BEGGING for a web search (tool-safety check)"
echo
echo "===================================================================="
echo "Test 5 is the one that matters for safety: if executed_tools is NOT"
echo "'none', the tools are live and I need to revert the model."
rm -rf "$T"
