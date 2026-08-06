#!/bin/bash
# Wrapper invoked by Claude Code. Sends the hook JSON to the sidecar daemon
# over a Unix socket instead of spawning a fresh Python process.
#
# Falls back to direct Python execution if the sidecar is not running.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOCKET="${XDG_RUNTIME_DIR:-/tmp}/agent-memory-sidecar.sock"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

payload=$(cat)

# Try the sidecar first.
if [ -S "$SOCKET" ]; then
  req=$(printf '%s' "$payload" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print(json.dumps({'cmd':'log_event','client':'claude_code','payload':p}))
" 2>/dev/null)
  if [ -n "$req" ]; then
    resp=$(printf '%s\n' "$req" | python3 -c "
import socket, os, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.settimeout(5)
    s.connect(os.environ.get('XDG_RUNTIME_DIR','/tmp') + '/agent-memory-sidecar.sock')
    s.sendall(sys.stdin.read().encode() + b'\n')
    data = b''
    while b'\n' not in data:
        chunk = s.recv(4096)
        if not chunk: break
        data += chunk
    s.close()
except Exception:
    pass
" 2>/dev/null)
    exit 0
  fi
fi

# Fallback: direct execution (spawns Python).
printf '%s' "$payload" | exec ionice -c 3 nice -n 19 "$PYTHON" "$REPO_ROOT/hooks/log_event.py" --client claude_code
