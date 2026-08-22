#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  LogiTrack — one-time setup for a fresh Mac.
#
#      bash "path/to/LogitrackV2/setup-mac.sh"
#
#  Installs Homebrew, Python, Node, and every package the tool needs, then
#  starts it. Safe to re-run: everything below is idempotent.
#
#  WHY PYTHON 3.12 AND NOT THE LATEST
#  engine_statsforecast/requirements.txt pins numpy~=1.26 and statsforecast~=1.7.
#  Neither publishes a wheel past cp312 (checked on PyPI: numpy 1.26.4 ships
#  cp39-cp312 only). On Python 3.13+ pip therefore tries to COMPILE numpy and
#  numba from source, which on a Mac without a full toolchain fails after a long
#  wall of compiler errors. `brew install python` gives you 3.13 today, so this
#  asks for python@3.12 by name and builds the venv from that exact interpreter.
#
#  WHY A VENV
#  Homebrew's Python is marked externally-managed (PEP 668), so a plain
#  `pip install` into it is refused outright. The venv also keeps this tool's
#  pinned numpy from fighting anything else on the machine.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$PROJ"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

[ -f package.json ] || die "Run this from inside the LogitrackV2 folder (no package.json here)."

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (it will ask for your Mac password)"
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# Put brew on PATH for THIS shell — Apple Silicon and Intel keep it in different places.
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && eval "$("$b" shellenv)" && break
done
command -v brew >/dev/null 2>&1 || die "Homebrew installed but isn't on PATH. Open a new Terminal and re-run."

# ── 2. Python 3.12 + Node ────────────────────────────────────────────────────
say "Installing Python 3.12 and Node (skipped if already present)"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list node        >/dev/null 2>&1 || brew install node

PY312="$(brew --prefix python@3.12)/bin/python3.12"
[ -x "$PY312" ] || die "python@3.12 installed but $PY312 is missing."

# ── 3. Backend packages, in a venv ───────────────────────────────────────────
say "Setting up the forecasting engine (a few minutes the first time)"
[ -d .venv ] || "$PY312" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install -r engine_statsforecast/requirements.txt

# ── 4. Frontend packages + build ─────────────────────────────────────────────
say "Setting up the browser app"
npm install
npm run build

# ── 5. Make the launcher double-clickable ────────────────────────────────────
chmod +x "Start LogiTrack.command" 2>/dev/null || true
chmod +x setup-mac.sh 2>/dev/null || true

# ── 6. Prove it works before claiming success ────────────────────────────────
say "Checking the install"
./.venv/bin/python - <<'PYCHECK'
import importlib.util, sys          # importlib.util is NOT pulled in by `import importlib`
missing = [m for m in ("fastapi","uvicorn","multipart","pandas","numpy",
                       "openpyxl","prophet","statsforecast","dateutil","requests")
           if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("Missing Python packages: " + ", ".join(missing))
import numpy, pandas
print(f"  Python {sys.version.split()[0]} · numpy {numpy.__version__} · pandas {pandas.__version__}")
PYCHECK
[ -f dist/index.html ] || die "The browser app didn't build (no dist/index.html)."
echo "  Browser app built."

say "Done — starting LogiTrack"
exec "$PROJ/Start LogiTrack.command"
