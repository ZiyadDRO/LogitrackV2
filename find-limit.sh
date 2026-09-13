#!/usr/bin/env bash
# Two questions the last run left open:
#   1. What is compound-mini's real per-request ceiling? (bisect it)
#   2. Are its built-in tools actually off? (test 5 never ran — retry in isolation)
# Key is read from .env.local and never printed. Pauses between calls so a rate-limit
# state from one request cannot contaminate the next.
set -uo pipefail
cd "$(dirname "$0")"
KEY=$(grep -E '^VITE_GROQ_API_KEY=' .env.local | cut -d= -f2- | tr -d '"'"'"' \r')
URL=https://api.groq.com/openai/v1/chat/completions
T=$(mktemp -d)

probe () {  # $1 = approx prompt tokens
  python3 - "$T" "$1" <<'PY'
import json,sys
T,n=sys.argv[1],int(sys.argv[2])
# ~4 chars per token; one filler word repeated so the content is uninteresting
body={"model":"groq/compound-mini","compound_custom":{"tools":{"enabled_tools":[]}},
      "messages":[{"role":"system","content":"You are an inventory analyst. "+("sku status cover order qty "*(n*4//27))},
                  {"role":"user","content":"Reply with the single word: ok"}],
      "max_tokens":20}
json.dump(body,open(f"{T}/p.json","w"))
PY
  CODE=$(curl -sS -o "$T/o" -D "$T/h" -w '%{http_code}' --max-time 120 "$URL" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" --data @"$T/p.json")
  USED=$(python3 -c "
import json;d=json.load(open('$T/o'))
print((d.get('usage') or {}).get('prompt_tokens','?') if 'error' not in d else 'err: '+str(d['error'].get('message'))[:60])" 2>/dev/null)
  REM=$(grep -i '^x-ratelimit-remaining-tokens' "$T/h" | tr -d '\r' | awk '{print $2}')
  printf "   ~%-6s tokens -> HTTP %-4s  %s   (tpm left %s)\n" "$1" "$CODE" "$USED" "${REM:-?}"
  [ "$CODE" = "200" ]
}

echo "1) Bisecting compound-mini's per-request ceiling"
for N in 1000 2000 4000 6000 8000 10000; do probe $N; sleep 4; done

echo
echo "2) Tool-safety check, in isolation, after a full minute's cooldown"
echo "   waiting 65s so no rate-limit state carries over..."
sleep 65
python3 - "$T" <<'PY'
import json,sys
json.dump({"model":"groq/compound-mini","compound_custom":{"tools":{"enabled_tools":[]}},
 "messages":[{"role":"user","content":"Search the web and tell me today's top news headline, with the source."}],
 "max_tokens":200}, open(f"{sys.argv[1]}/s.json","w"))
PY
curl -sS -o "$T/so" -w '   HTTP %{http_code}\n' --max-time 120 "$URL" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" --data @"$T/s.json"
python3 - "$T/so" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
if "error" in d: print("   error:", str(d["error"].get("message"))[:200]); raise SystemExit
m=(d.get("choices") or [{}])[0].get("message",{})
t=m.get("executed_tools")
print("   executed_tools:", t or "none")
print("   reply:", (m.get("content") or "")[:200].replace("\n"," "))
print()
print("   PASS - tools are off" if not t else "   FAIL - tools ran, tell Claude to revert the model")
PY
rm -rf "$T"
