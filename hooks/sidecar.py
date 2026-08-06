#!/usr/bin/env python3
"""
Persistent sidecar daemon for agent-memory-hooks-neo4j.

Replaces per-event spawn() with a single long-running process that:
  - Maintains a persistent Neo4j driver (connection pool)
  - Listens on a Unix socket for JSON-line requests
  - Handles log_event and inject_memory commands
  - Returns JSON-line responses on the same connection

Protocol (newline-delimited JSON over Unix socket):
  Request:  {"cmd": "log_event"|"inject_memory", "client": "opencode", "payload": {...}}
  Response: {"ok": true} | {"ok": true, "result": {...}} | {"ok": false, "error": "..."}

Usage:
  python sidecar.py                    # foreground
  python sidecar.py --daemon           # fork to background, write pidfile

Socket path: $XDG_RUNTIME_DIR/agent-memory-sidecar.sock  (or /tmp/...)
"""

import argparse
import json
import os
import signal
import socketserver
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

SOCKET_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
SOCKET_PATH = os.path.join(SOCKET_DIR, "agent-memory-sidecar.sock")
PIDFILE = os.path.join(SOCKET_DIR, "agent-memory-sidecar.pid")

MAX_RESPONSE_CHARS = 4000
MAX_PROMPT_HITS = 5
MIN_FULLTEXT_SCORE = 0.5

import re

STOPWORDS = {
    "this", "that", "with", "from", "have", "what", "when", "where", "which",
    "would", "could", "should", "your", "their", "there", "about", "into",
    "they", "them", "then", "than", "some", "make", "like", "want", "need",
    "just", "only", "also", "still", "very", "much", "more", "most", "ours",
    "please", "thanks", "code", "file", "files",
}

# ---------------------------------------------------------------------------
# Neo4j driver (singleton, persistent connection pool)
# ---------------------------------------------------------------------------

_driver = None
_driver_lock = threading.Lock()


def get_driver():
    global _driver
    if _driver is None:
        with _driver_lock:
            if _driver is None:
                _driver = GraphDatabase.driver(
                    NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
                    max_connection_lifetime=3600,
                    connection_acquisition_timeout=5,
                )
    return _driver


# ---------------------------------------------------------------------------
# log_event  (fire-and-forget, same logic as hooks/log_event.py)
# ---------------------------------------------------------------------------

def _serialize_tool_response(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + f"...[truncated {len(text) - MAX_RESPONSE_CHARS} chars]"
    return text


def _read_transcript(path):
    if not path:
        return None
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return None


def ensure_constraints(tx):
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_id IS UNIQUE")
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE")
    tx.run("CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS FOR (m:Memory) ON EACH [m.content, m.path]")


def _append_event(tx, session_id, client, event_props):
    tx.run(
        """
        MERGE (s:Session {session_id: $session_id})
        ON CREATE SET s.created_at = $timestamp, s.client = $client
        SET s.client = coalesce(s.client, $client)
        WITH s
        CREATE (e:Event $event_props)
        WITH s, e
        OPTIONAL MATCH (s)-[old_latest:LATEST_EVENT]->(prev:Event)
        DELETE old_latest
        WITH s, e, prev
        FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
            CREATE (prev)-[:NEXT]->(e)
        )
        FOREACH (_ IN CASE WHEN prev IS NULL THEN [1] ELSE [] END |
            CREATE (s)-[:FIRST_EVENT]->(e)
        )
        CREATE (s)-[:LATEST_EVENT]->(e)
        """,
        session_id=session_id, client=client,
        timestamp=event_props.get("timestamp"), event_props=event_props,
    )


def handle_log_event(data: dict, client: str):
    session_id = data.get("session_id", "unknown")
    event_name = data.get("hook_event_name", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()
    event_id = f"{client}_{session_id}_{timestamp}_{event_name}"

    event_props = {
        "event_id": event_id,
        "event_name": event_name,
        "client": client,
        "timestamp": timestamp,
        "cwd": data.get("cwd"),
        "tool_name": data.get("tool_name"),
        "tool_use_id": data.get("tool_use_id"),
        "tool_input": json.dumps(data.get("tool_input")) if data.get("tool_input") else None,
        "tool_response": _serialize_tool_response(data.get("tool_response"))
        if data.get("tool_response") is not None else None,
        "prompt": data.get("prompt"),
        "model": data.get("model"),
        "source": data.get("source"),
        "turn_id": data.get("turn_id"),
        "last_assistant_message": data.get("last_assistant_message"),
        "stop_hook_active": data.get("stop_hook_active"),
        "transcript_path": data.get("transcript_path"),
        "transcript": _read_transcript(data.get("transcript_path")),
    }
    event_props = {k: v for k, v in event_props.items() if v is not None}

    driver = get_driver()
    with driver.session() as session:
        session.execute_write(ensure_constraints)
        session.execute_write(_append_event, session_id, client, event_props)


# ---------------------------------------------------------------------------
# inject_memory  (returns context, same logic as hooks/inject_memory.py)
# ---------------------------------------------------------------------------

def _fulltext_search(session, query, limit=MAX_PROMPT_HITS):
    cypher = """
    CALL db.index.fulltext.queryNodes('memory_fulltext', $query)
    YIELD node, score
    WHERE score > $min_score
    RETURN node.path AS path, node.content AS content, score
    ORDER BY score DESC
    LIMIT $limit
    """
    return list(session.run(cypher, query=query, min_score=MIN_FULLTEXT_SCORE, limit=limit))


def _extract_terms(prompt):
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", prompt.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def handle_inject_memory(data: dict):
    event = data.get("hook_event_name", "")
    normalized = event.lower()

    context = ""
    if normalized in {"sessionstart", "session_start"}:
        driver = get_driver()
        with driver.session() as s:
            profile = list(s.run(
                "MATCH (m:Memory) WHERE m.path STARTS WITH 'profile/' "
                "RETURN m.path AS path, m.content AS content ORDER BY m.path LIMIT 5"
            ))
        if profile:
            parts = ["# Memory (from prior sessions)\n", "## Profile\n"]
            for r in profile:
                parts.append(f"### {r['path']}\n{r['content']}\n")
            context = "\n".join(parts)

    elif normalized in {"userpromptsubmit", "beforesubmitprompt"}:
        prompt = data.get("prompt", "").strip()
        if prompt:
            driver = get_driver()
            with driver.session() as s:
                rows = _fulltext_search(s, prompt)
                if not rows:
                    terms = _extract_terms(prompt)
                    if terms:
                        rows = _fulltext_search(s, " OR ".join(terms))
            if rows:
                parts = ["# Relevant memory for this prompt\n"]
                for r in rows:
                    parts.append(f"## {r['path']}\n{r['content']}\n")
                context = "\n".join(parts)

    if not context.strip():
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

class RequestHandler(socketserver.StreamRequestHandler):
    """Handle one connection: read JSON lines, process, write JSON line back."""

    def handle(self):
        try:
            for raw_line in self.rfile:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    resp = dispatch(req)
                except Exception as e:
                    resp = {"ok": False, "error": str(e)}
                self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass  # client disconnected, nothing to do


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def dispatch(req: dict) -> dict:
    cmd = req.get("cmd", "")
    client = req.get("client", "opencode")
    payload = req.get("payload", {})

    try:
        if cmd == "log_event":
            handle_log_event(payload, client)
            return {"ok": True}
        elif cmd == "inject_memory":
            result = handle_inject_memory(payload)
            return {"ok": True, "result": result}
        else:
            return {"ok": False, "error": f"unknown cmd: {cmd}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cleanup(*_):
    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass
    try:
        os.unlink(PIDFILE)
    except OSError:
        pass
    global _driver
    if _driver:
        _driver.close()
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="agent-memory sidecar daemon")
    parser.add_argument("--daemon", action="store_true", help="fork to background")
    args = parser.parse_args()

    # Clean up stale socket.
    if os.path.exists(SOCKET_PATH):
        # Check if another sidecar is listening.
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SOCKET_PATH)
            sock.close()
            print(f"Sidecar already running on {SOCKET_PATH}", file=sys.stderr)
            sys.exit(0)
        except ConnectionRefusedError:
            os.unlink(SOCKET_PATH)
        finally:
            sock.close()

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            print(f"Sidecar forked to PID {pid}, socket: {SOCKET_PATH}")
            sys.exit(0)
        os.setsid()

    # Write pidfile.
    Path(PIDFILE).write_text(str(os.getpid()))

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # Warm up the driver (fail fast if Neo4j is unreachable).
    try:
        get_driver().verify_connectivity()
        print(f"Connected to Neo4j at {NEO4J_URI}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Neo4j not reachable ({e}), will retry on first request", file=sys.stderr)

    server = ThreadedUnixServer(SOCKET_PATH, RequestHandler)
    os.chmod(SOCKET_PATH, 0o600)
    print(f"Sidecar listening on {SOCKET_PATH} (PID {os.getpid()})", file=sys.stderr)

    try:
        server.serve_forever()
    finally:
        cleanup()


if __name__ == "__main__":
    main()
