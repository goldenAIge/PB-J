#!/bin/bash
# Start crypto latency bot with streak mode
# Usage: ./scripts/bash/start_bot.sh [streak_balance] [streak_wins]
#   e.g. ./scripts/bash/start_bot.sh 36.76 1

STREAK_BALANCE="${1:-20.0}"
STREAK_WINS="${2:-0}"

PROJECT_DIR="/Users/pablo/Documents/PB&J"
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR"

# Use the project virtualenv — all deps are pinned here
PYTHON="$PROJECT_DIR/venv/bin/python"

if [ ! -f "$PYTHON" ]; then
    echo "ERROR: virtualenv not found at $PROJECT_DIR/venv"
    echo "Run: python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

# Check if bot is already running
if ps aux | grep "[c]li.py run-crypto-latency" > /dev/null; then
    echo "Bot is already running!"
    ps aux | grep "[c]li.py run-crypto-latency"
    exit 1
fi

echo "Starting crypto latency bot..."
echo "  Streak balance: \$${STREAK_BALANCE}, Wins: ${STREAK_WINS}"

nohup "$PYTHON" scripts/python/cli.py run-crypto-latency \
    --no-dry-run \
    --assets btc,eth,sol \
    --windows 5,15 \
    --min-move 0.4 \
    --max-entry 0.80 \
    --streak \
    --streak-balance "$STREAK_BALANCE" \
    --streak-wins "$STREAK_WINS" \
    >> crypto_latency_live.log 2>&1 &

echo "Bot started with PID $!"
echo "Logs: tail -f crypto_latency_live.log"
