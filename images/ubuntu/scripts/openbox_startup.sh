#!/bin/bash
echo "Starting openbox..."
DISPLAY=:${DISPLAY_NUM} openbox > /tmp/openbox_stderr.log 2>&1 &
OPENBOX_PID=$!
sleep 0.5
kill -0 $OPENBOX_PID 2>/dev/null && echo "openbox started (PID: ${OPENBOX_PID})" && exit 0
echo "openbox failed" >&2 && cat /tmp/openbox_stderr.log >&2 && exit 1
