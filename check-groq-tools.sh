#!/usr/bin/env bash
# Does groq/compound-mini actually honour enabled_tools: [] ?
#
# Groq documents enabled_tools as a way to NARROW the built-in tool set. It does not
# document what an empty array does. The Ask AI drawer relies on it being "none", so
# this asks a question that begs for a web search and reports whether one ran.
#
# Your key is read from .env.local and never printed.
set -euo pipefail
cd "$(dirname "$0")"
KEY=$(grep -E '^VITE_GROQ_API_KEY=' .env.local | cut -d= -f2- | tr -d '"'"'"' \r')
[ -n "$KEY" ] || { echo "No VITE_GROQ_API_KEY found in .env.local"; exit 1; }

curl -s https://api.groq.com/openai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"groq/compound-mini",
       "compound_custom":{"tools":{"enabled_tools":[]}},
       "messages":[{"role":"user","content":"Search the web and tell me the top news headline right now."}],
       "max_tokens":200}' \
| python3 -c '
import sys, json
d = json.load(sys.stdin)
if d.get("error"): print("ERROR:", d["error"].get("message")); sys.exit(1)
m = (d.get("choices") or [{}])[0].get("message", {})
t = m.get("executed_tools")
print("executed_tools :", t if t else "none")
print("reply          :", (m.get("content") or "")[:220].replace("\n"," "))
print()
print("PASS - tools are off, safe to use" if not t else "FAIL - tools ran anyway, tell Claude")
'
