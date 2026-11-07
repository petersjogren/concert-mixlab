#!/bin/bash
# Start the concert-mixlab GUI (http://127.0.0.1:8765) and open it in the browser.
cd "$(dirname "$0")"
PORT=${PORT:-8765}
if ! curl -s "http://127.0.0.1:$PORT/api/version" >/dev/null; then
  nohup .venv/bin/python server.py --port "$PORT" > server.log 2>&1 &
  for i in $(seq 1 30); do curl -s "http://127.0.0.1:$PORT/api/version" >/dev/null && break; sleep 0.3; done
fi
[ "$1" = "--no-open" ] || open "http://127.0.0.1:$PORT"
echo "concert-mixlab GUI: http://127.0.0.1:$PORT  (log: $(pwd)/server.log)"
