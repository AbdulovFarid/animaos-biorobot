#!/usr/bin/env bash
set -u

PROJECT_DIR="/home/fargo/PycharmProjects/Biorobot"
ENV_FILE="/home/fargo/.config/biorobot/groq.env"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
BRIDGE_SCRIPT="$PROJECT_DIR/luanti_bridge.py"
BRIDGE_LOG="/tmp/aya_bridge.log"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[AYA] Не найден Python: $PYTHON_BIN" >&2
    exit 1
fi
if [[ ! -f "$BRIDGE_SCRIPT" ]]; then
    echo "[AYA] Не найден мост: $BRIDGE_SCRIPT" >&2
    exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

export AYA_HEARTBEAT_GRACE_PERIOD="${AYA_HEARTBEAT_GRACE_PERIOD:-180}"
export PYTHONUNBUFFERED=1

cd "$PROJECT_DIR" || exit 1

"$PYTHON_BIN" -u "$BRIDGE_SCRIPT" >>"$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

cleanup() {
    if kill -0 "$BRIDGE_PID" 2>/dev/null; then
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "[AYA] Мост запущен (PID $BRIDGE_PID). Запускаю Luanti..."
flatpak run org.luanti.luanti "$@" &
GAME_PID=$!

wait "$GAME_PID"
GAME_STATUS=$?
echo "[AYA] Luanti завершён; мост останавливается."
exit "$GAME_STATUS"
