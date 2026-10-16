#!/usr/bin/env bash
# One-click launcher for the macOS Agent Demo Studio (demo_hub).
#
#   ./start_demo.sh            # start on :8800
#   PORT=8810 ./start_demo.sh  # start on a custom port
#   MOBILE=1 ./start_demo.sh   # enable web ADB/scrcpy Android preview
#
# It (re)starts demo_hub detached (survives this shell), waits until it is up,
# then prints the two URLs you need plus a short OBS recording recipe.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"                  # repo root
PORT="${PORT:-8800}"
DEMO_ARGS=()
if [ "${MOBILE:-0}" = "1" ]; then DEMO_ARGS+=(--mobile); fi
PY="$REPO/.venv/bin/python"
LOG="/tmp/demo_hub_${PORT}.log"
BASE="http://127.0.0.1:${PORT}"

cd "$REPO"

if [ ! -x "$PY" ]; then
  echo "ERROR: venv python not found at $PY" >&2
  echo "  create it with:  uv venv --python 3.12" >&2
  echo "  install packages: uv pip install -e ./packages/sdk -e ./packages/harness/runtime" >&2
  exit 1
fi

# mac_agent also resolves api_key from packages/harness/configs/local.json. Do not parse or
# print that file here: this launcher only provides a useful hint for env/file users.
if [ -z "${DASHSCOPE_API_KEY:-}" ] && [ -z "${API_KEY:-}" ] && [ ! -s "$HERE/.api_key" ]; then
  echo "INFO: no API key in the environment or $HERE/.api_key." >&2
  echo "      The agent may still use api_key from packages/harness/configs/local.json or a keyless endpoint." >&2
fi

# Restart only our own demo hub. Never kill an unrelated service merely because it
# happens to own the requested port.
OLD="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$OLD" ]; then
  for pid in $OLD; do
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    case "$command" in
      *"$HERE/demo_hub.py"*)
        echo "Stopping existing demo_hub on :$PORT (pid $pid)"
        kill "$pid" 2>/dev/null || true
        ;;
      *)
        echo "ERROR: :$PORT is owned by another process (pid $pid): $command" >&2
        echo "       Choose another port, for example: PORT=$((PORT + 1)) $0" >&2
        exit 1
        ;;
    esac
  done
  sleep 2
fi

echo "Starting demo_hub on :$PORT (log: $LOG) ..."
nohup "$PY" -B "$HERE/demo_hub.py" --port "$PORT" --no-open "${DEMO_ARGS[@]}" > "$LOG" 2>&1 &
disown || true

for _ in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 "$BASE/"; then break; fi
  sleep 0.5
done

if ! curl -s -o /dev/null --max-time 2 "$BASE/"; then
  echo "ERROR: demo_hub did not come up; last log lines:" >&2
  tail -20 "$LOG" >&2 || true
  exit 1
fi

cat <<EOF

✅ demo_hub is up (detached; it keeps running after this shell exits).

   Workspace (drive the agent) : $BASE/
   OBS HUD overlay (transparent): $BASE/panel
   Log                          : $LOG
   Stop it                      : lsof -nP -tiTCP:$PORT -sTCP:LISTEN | xargs kill

OBS recording recipe (clean, QuickTime-playable — no in-browser tab capture):
   1) Source ▸ macOS Screen Capture  → pick the display the agent operates on.
   2) Source ▸ Browser  → URL $BASE/panel
        • set Width/Height to the HUD region you want (e.g. 760×980)
        • UNCHECK "Shutdown source when not visible" (keeps it polling)
        • drag it over the screen capture; transparent bg shows the screen through.
   3) Settings ▸ Output ▸ Recording: format MP4, encoder VideoToolbox (or x264).
   4) Drive the agent from $BASE/ on ANOTHER monitor/corner so the workspace
      itself is not in the captured screen.
   5) For visible clicks, tell the agent: "本任务全程用前台真实鼠标键盘操作，方便录制演示"
      (default is element-first AX, which clicks in the background = invisible).

EOF
