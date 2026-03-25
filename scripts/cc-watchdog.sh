#!/usr/bin/env bash

THRESHOLD=${1:-70}
INTERVAL=${2:-60}

echo "👁 Claude Code watchdog started"
echo "   Threshold : ${THRESHOLD}% of 7-day window"
echo "   Check every: ${INTERVAL}s"
echo "   Press Ctrl+C to stop"
echo ""

while true; do
    OUTPUT=$(npx cclimits --claude 2>/dev/null)

    USED=$(echo "$OUTPUT" | grep -A2 "7-Day Window" | grep "Used:" | grep -oP '\d+\.\d+')

    if [[ -z "$USED" ]]; then
        echo "[$(date +%H:%M:%S)] ⚠ Could not parse 7-day usage"
        sleep "$INTERVAL"
        continue
    fi

    echo "[$(date +%H:%M:%S)] 7-day usage: ${USED}%"

    if (( $(echo "$USED >= $THRESHOLD" | bc -l) )); then
        echo ""
        echo "🛑 Threshold reached (${USED}% >= ${THRESHOLD}%) — killing Claude Code..."

        pkill -f "claude" && echo "✅ Claude Code process killed." || echo "⚠ No Claude Code process found."
        exit 0
    fi

    sleep "$INTERVAL"
done