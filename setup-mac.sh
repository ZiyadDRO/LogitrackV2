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
# Prophet is installed from the requirements file below like everything else; what it
# needs is the cmdstanpy upper bound pinned there (see the note in requirements.txt —
# cmdstanpy 1.3.0 rejects the trimmed cmdstan tree Prophet's wheel ships, and Prophet
# reports it as a missing `stan_backend` attribute). --only-binary is belt-and-braces
# for a second failure mode: with no wheel for this platform pip would fall back to the
# sdist and try to compile Stan, which fails on a Mac without a full toolchain. Better
# to fail here, loudly, than to install something that can't forecast.
./.venv/bin/python -m pip install --only-binary=:all: -r engine_statsforecast/requirements.txt

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

# find_spec only proves the FILES are on disk. A Prophet installed from source with a
# failed Stan build passes that test and then throws on the first fit, which is how a
# "successful" setup turned into products silently dropping out of the forecast. The
# only check worth making is the thing the app actually does: fit a model.
import logging, warnings
logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")
try:
    from prophet import Prophet
    df = pandas.DataFrame({"ds": pandas.date_range("2025-01-01", periods=90),
                           "y": numpy.linspace(50, 60, 90)})
    Prophet(weekly_seasonality=False, yearly_seasonality=False).fit(df)
except Exception as e:
    sys.exit("Prophet installed but can't fit a model (" + type(e).__name__ + ": " + str(e)[:120] + ").\n"
             "  Almost always the cmdstanpy pin: 1.3.0+ rejects the cmdstan tree Prophet ships.\n"
             "  Repair it with:\n"
             "    ./.venv/bin/python -m pip install --force-reinstall --no-cache-dir \\\n"
             "        --only-binary=:all: prophet 'cmdstanpy>=1.2,<1.3'")
print("  Prophet fits a model.")
PYCHECK
[ -f dist/index.html ] || die "The browser app didn't build (no dist/index.html)."
echo "  Browser app built."

say "Done — starting LogiTrack"
exec "$PROJ/Start LogiTrack.command"
