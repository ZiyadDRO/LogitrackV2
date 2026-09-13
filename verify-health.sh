#!/usr/bin/env bash
# Does the new startup check actually detect a retired model?
# Lists what your key can reach, then checks the configured ids against it.
# Key read from .env.local, never printed.
set -uo pipefail
cd "$(dirname "$0")"
KEY=$(grep -E '^VITE_GROQ_API_KEY=' .env.local | cut -d= -f2- | tr -d '"'"'"' \r')
curl -sS --max-time 60 https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $KEY" \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" -o /tmp/models.json
python3 - <<'PY'
import json, re, io
d = json.load(open('/tmp/models.json'))
if 'error' in d: print("models endpoint error:", d['error'].get('message')); raise SystemExit(1)
avail = sorted(m['id'] for m in d.get('data', []) if m.get('id'))
print(f"{len(avail)} models reachable with your key\n")

src = io.open('src/lib/ai.js', encoding='utf-8').read()
cfg = dict(re.findall(r'export const (GROQ_MODEL(?:_CHAT)?) = "([^"]+)"', src))
print("configured in src/lib/ai.js:")
for k, v in cfg.items():
    print(f"  {k:16} {v:28} {'OK, reachable' if v in avail else 'MISSING -> banner fires'}")

print("\ncontrol: models this project used before, which Groq has retired")
for dead in ("llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct"):
    print(f"  {dead:42} {'still listed' if dead in avail else 'gone — check would have caught it'}")

miss = [v for v in cfg.values() if v not in avail]
print()
print("VERDICT:", "no banner, all configured models live" if not miss
      else f"banner would fire for: {', '.join(miss)}")
PY
