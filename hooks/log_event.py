#!/usr/bin/env python3
"""
Shared hook script that logs events to Neo4j as a linked list per session.

Used by both Claude Code (.claude/hooks/) and Codex (.codex/hooks/) — pass
--client to tag the originating tool. The hook payloads from the two clients
share almost all fields (session_id, hook_event_name, transcript_path, cwd,
tool_name, tool_input, tool_response, prompt, model, source); Codex adds
turn_id, tool_use_id, last_assistant_message, stop_hook_active.

Graph: (Session)-[:FIRST_EVENT]->(Event)-[:NEXT]->(Event)->...
       (Session)-[:LATEST_EVENT]->(last Event)
"""

import argparse
import json
import sys
import os
from datetime import datetime, timezone

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


MAX_RESPONSE_CHARS = 4000


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


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


def log_event(data: dict, client: str):
    session_id = data.get("session_id", "unknown")
    event_name = data.get("hook_event_name", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    # Namespace the event_id by client so different clients can't collide on
    # the same session_id.
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
        if data.get("tool_response") is not None
        else None,
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
    driver.close()


def _append_event(tx, session_id: str, client: str, event_props: dict):
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
        session_id=session_id,
        client=client,
        timestamp=event_props.get("timestamp"),
        event_props=event_props,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, choices=["claude_code", "codex", "cursor", "opencode"])
    args = parser.parse_args()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        log_event(data, client=args.client)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
