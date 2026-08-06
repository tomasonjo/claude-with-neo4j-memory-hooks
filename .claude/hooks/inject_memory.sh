#!/bin/bash
# Wrapper invoked by Claude Code for the memory injector. Sends to the sidecar
# daemon over a Unix socket instead of spawning a fresh Python process.
#
# Falls back to direct Python execution if the sidecar is not running.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOCKET="${XDG_RUNTIME_DIR:-/tmp}/agent-memory-sidecar.sock"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

payload=$(cat)

# Try the sidecar first.
if [ -S "$SOCKET" ]; then
  result=$(printf '%s' "$payload" | python3 -c "
import socket, os, sys, json
p = json.load(sys.stdin)
req = json.dumps({'cmd':'inject_memory','client':'claude_code','payload':p})
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.settimeout(5)
    s.connect(os.environ.get('XDG_RUNTIME_DIR','/tmp') + '/agent-memory-sidecar.sock')
    s.sendall(req.encode() + b'\n')
    data = b''
    while b'\n' not in data:
        chunk = s.recv(65536)
        if not chunk: break
        data += chunk
    s.close()
    resp = json.loads(data.decode().strip())
    if resp.get('ok') and resp.get('result'):
        print(json.dumps(resp['result']))
except Exception:
    pass
" 2>/dev/null)
  if [ -n "$result" ]; then
    printf '%s' "$result"
    exit 0
  fi
fi

# Fallback: direct execution (spawns Python).
printf '%s' "$payload" | exec ionice -c 3 nice -n 19 "$PYTHON" "$REPO_ROOT/hooks/inject_memory.py" --client claude_code
