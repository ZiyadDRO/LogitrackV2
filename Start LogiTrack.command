#!/bin/bash
# LogiTrack launcher - double-click this file to start the app.
# Starts the backend and browser page, opens your browser, and shuts both down
# when you close this window or press Ctrl+C.

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
if   [ -f "$SELF_DIR/package.json" ];                            then PROJ="$SELF_DIR"
elif [ -f "/Users/ziyadali/Desktop/LogitrackV2/package.json" ]; then PROJ="/Users/ziyadali/Desktop/LogitrackV2"
else
  echo "Could not find the LogiTrack project."
  echo "Move this file into your LogitrackV2 folder, or edit the path inside it."
  read -r -p "Press Enter to close..."; exit 1
fi
cd "$PROJ" || exit 1

FRONT_HOST="127.0.0.1"
BACK_HOST="127.0.0.1"
FRONT_URL="http://$FRONT_HOST:5173"
BACK_URL="http://$BACK_HOST:8000"

echo "============================================"
echo "  Starting LogiTrack"
echo "  $PROJ"
echo "============================================"

cleanup() {
  code=$?
  trap - INT TERM HUP EXIT
  echo ""
  echo "Shutting down LogiTrack..."
  [ -n "$BACK_PID" ]  && kill "$BACK_PID"  2>/dev/null
  [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null
  pkill -f "uvicorn main:app.*engine_statsforecast" 2>/dev/null
  pkill -f "$PROJ/node_modules/.bin/vite" 2>/dev/null
  pkill -f "http.server 5173.*--directory dist" 2>/dev/null
  pkill -f "serve_dist.py" 2>/dev/null
  exit "$code"
}
trap cleanup INT TERM HUP EXIT

kill_port_if_logitrack() {
  port="$1"
  for pid in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u); do
    cmd="$(ps -p "$pid" -o command= 2>/dev/null)"
    case "$cmd" in
      *"$PROJ"*|*"uvicorn main:app"*|*"http.server 5173"*|*"serve_dist.py"*|*"vite"*)
        kill "$pid" 2>/dev/null
        ;;
    esac
  done
}

wait_for_url() {
  label="$1"
  url="$2"
  tries="$3"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "$label is ready."
      return 0
    fi
    sleep 1
  done
  echo "$label did not start at $url."
  return 1
}

echo "Clearing any old LogiTrack server windows..."
kill_port_if_logitrack 8000
kill_port_if_logitrack 5173
pkill -f "uvicorn main:app.*engine_statsforecast" 2>/dev/null
pkill -f "$PROJ/node_modules/.bin/vite" 2>/dev/null
pkill -f "http.server 5173.*--directory dist" 2>/dev/null
sleep 1

if [ ! -d node_modules ]; then
  echo "Installing browser app packages. This can take a minute..."
  npm install || { echo "Install failed. Is Node.js installed?"; read -r -p "Press Enter to close..."; exit 1; }
fi

# Prefer the project's own virtual environment. setup-mac.sh builds it from
# Python 3.12 specifically: the pinned numpy~=1.26 and statsforecast~=1.7 publish
# no wheels past cp312, so on a current Mac a bare `python3` is 3.13 and pip would
# try to compile them from source. It also can't be pip-installed into at all —
# Homebrew's Python is externally managed (PEP 668).
if [ -x "$PROJ/.venv/bin/python" ]; then
  PY="$PROJ/.venv/bin/python"
else
  PY="python3"
  command -v "$PY" >/dev/null 2>&1 || PY="python"
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3 was not found. Run:  bash \"$PROJ/setup-mac.sh\""
    read -r -p "Press Enter to close..."; exit 1
  fi
fi

if ! "$PY" -c "import uvicorn, fastapi, multipart, pandas, numpy, openpyxl, prophet, statsforecast" >/dev/null 2>&1; then
  if [ -x "$PROJ/.venv/bin/python" ]; then
    echo "Installing/repairing backend packages. This can take a few minutes..."
    "$PY" -m pip install -r engine_statsforecast/requirements.txt \
      || { echo "Backend package install failed."; read -r -p "Press Enter to close..."; exit 1; }
  else
    # No venv: don't pip-install into a system Python that will refuse it or is
    # the wrong version. Send them to the installer, which handles both.
    echo "Backend packages are missing and there's no virtual environment yet."
    echo "Run:  bash \"$PROJ/setup-mac.sh\""
    read -r -p "Press Enter to close..."; exit 1
  fi
fi

if [ ! -f dist/index.html ] || find src public -type f -newer dist/index.html -print -quit | grep -q . || find package.json vite.config.js .env.local -newer dist/index.html -print -quit 2>/dev/null | grep -q .; then
  # .env.local is checked too: the Groq key is baked into the browser build, so a new
  # key means nothing until the app is rebuilt with it.
  echo "Updating browser app files..."
  npm run build || { echo "Browser app build failed."; read -r -p "Press Enter to close..."; exit 1; }
fi

echo "Backend  : $BACK_URL"
# Capture backend output to a log so that if it fails to start, we can SHOW the
# reason instead of the window vanishing before it can be read.
"$PY" -m uvicorn main:app --app-dir engine_statsforecast --host "$BACK_HOST" --port 8000 > "$PROJ/backend.log" 2>&1 &
BACK_PID=$!

echo "Frontend : $FRONT_URL"
# Custom static server (serve_dist.py) sets no-cache on index.html so a rebuild is
# always picked up — no more manually clearing the browser cache. Falls back to the
# stock server if the script is somehow missing.
if [ -f "$PROJ/serve_dist.py" ]; then
  "$PY" "$PROJ/serve_dist.py" 5173 "$FRONT_HOST" dist > "$PROJ/frontend.log" 2>&1 &
else
  "$PY" -m http.server 5173 --bind "$FRONT_HOST" --directory dist > "$PROJ/frontend.log" 2>&1 &
fi
FRONT_PID=$!

if ! wait_for_url "Backend" "$BACK_URL/api/health" 60; then
  echo ""
  echo "!! The backend never came up on $BACK_URL — this is why saving/categorizing fails."
  echo "   Most common causes: port 8000 already in use, or a missing Python package."
  echo "   Backend log (last 40 lines):"
  echo "   ------------------------------------------------------------"
  tail -40 "$PROJ/backend.log" 2>/dev/null | sed 's/^/   /'
  echo "   ------------------------------------------------------------"
  read -r -p "Press Enter to close..."
  exit 1
fi
if ! wait_for_url "Frontend" "$FRONT_URL/" 30; then
  echo ""
  echo "!! The frontend never came up on $FRONT_URL. Frontend log (last 40 lines):"
  echo "   ------------------------------------------------------------"
  tail -40 "$PROJ/frontend.log" 2>/dev/null | sed 's/^/   /'
  echo "   ------------------------------------------------------------"
  read -r -p "Press Enter to close..."
  exit 1
fi

open "$FRONT_URL"

echo ""
echo "LogiTrack is running. Leave this window open."
echo "Close it or press Ctrl+C to stop the app."
echo ""

while true; do
  if ! kill -0 "$BACK_PID" 2>/dev/null; then
    echo "The backend stopped unexpectedly."
    exit 1
  fi
  if ! kill -0 "$FRONT_PID" 2>/dev/null; then
    echo "The frontend stopped unexpectedly."
    exit 1
  fi
  sleep 2
done
