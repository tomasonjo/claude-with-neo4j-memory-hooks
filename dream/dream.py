#!/usr/bin/env python3
"""
Dream phase: read recent session events from Neo4j, ask Claude to distill
them into durable memories, write them back.

Memories imitate markdown files: each :Memory node has a `path` (e.g.
"profile/role.md", "tools/bash/grep-flags.md") and a `content` field holding
the full markdown body (frontmatter + prose).

Schema:
    (:Memory {path, content, updated_at})         -- path is unique
    (:Memory)-[:DERIVED_FROM]->(:Session)

Usage:
    python dream.py                  # dream over sessions not yet dreamed
    python dream.py --session <id>   # dream over one session
    python dream.py --since 24h      # only events newer than 24h / 7d / 30m
    python dream.py --dry-run        # print, don't write

Authentication backend (--auth / DREAM_AUTH, default: sdk):
    sdk  — Anthropic SDK; requires ANTHROPIC_API_KEY (original behaviour).
    cli  — claude CLI in non-interactive mode (-p); uses the OAuth session
           from an existing Claude Code installation.  Set DREAM_CLAUDE_BIN
           to override the binary path resolved via PATH.

Chunked processing (--chunk-size / DREAM_CHUNK_SIZE, default: 0 = off):
    Split oversized sessions into overlapping windows of N events so they
    fit within the model context window.  Each chunk receives the memories
    accumulated by all previous chunks as its "existing memories" context,
    letting Claude merge and refine rather than duplicate.  When chunking
    is enabled, --max-events is only applied AFTER chunking fails (i.e. a
    chunk itself is too large); without chunking, --max-events still causes
    the whole session to be skipped.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta

try:
    from anthropic import Anthropic as _Anthropic
except ImportError:  # package not installed — CLI path will be used
    _Anthropic = None  # type: ignore[assignment,misc]

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MODEL = os.environ.get("DREAM_MODEL", "claude-opus-4-5")
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are the "dream phase" for a Claude Code memory system. \
You receive a chronological log of hook events from a Claude Code session \
(SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop) plus the set of \
markdown memories that already exist. Distill the session into durable markdown \
memories that will help future sessions.

Each memory imitates a markdown file: it has a path and a markdown body with \
YAML frontmatter. Organize paths semantically by topic, e.g.:

  profile/role.md
  profile/preferences.md
  tools/bash/common-flags.md
  tools/edit/conventions.md
  project/<short-slug>.md
  general/<short-slug>.md

Output STRICT JSON only, no prose, matching this schema:

{
  "memories": [
    {
      "path": "profile/role.md",
      "content": "---\\ntitle: User role\\nkind: profile\\n---\\n\\n<markdown body>"
    }
  ]
}

Frontmatter must include `title` and `kind` (one of: profile, tool, project, general).
The body should be tight markdown a future agent can read cold.

Rules:
- If a memory at the same path already exists, return an UPDATED full body that \
merges new evidence with the prior content. Do not duplicate facts. Remove anything \
the new events contradict.
- Skip ephemeral details (one-off filenames, debug output) and anything obvious \
from a fresh repo read (paths, git history).
- Prefer fewer, sharper memories over many vague ones.
- If nothing is worth remembering, return {"memories": []}.
- Each memory must stand alone — a future agent reads it without this transcript."""


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdm])", s)
    if not m:
        raise ValueError(f"--since must look like '24h', '7d', '30m'; got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return datetime.now(timezone.utc) - delta


def fetch_events(driver, session_id: str | None, since: datetime | None):
    """Return list of (session_id, [event_props, ...]) ordered chronologically.

    A session is included if it has at least one event newer than its
    `last_dreamed_at` watermark (or has never been dreamed).
    """
    where, params = ["(s.last_dreamed_at IS NULL OR e.timestamp > s.last_dreamed_at)"], {}
    if session_id:
        where.append("s.session_id = $session_id")
        params["session_id"] = session_id
    if since:
        where.append("e.timestamp >= $since")
        params["since"] = since.isoformat()

    query = f"""
    MATCH (s:Session)-[:FIRST_EVENT|NEXT*0..]->(e:Event)
    WHERE {' AND '.join(where)}
    RETURN s.session_id AS session_id, e
    ORDER BY s.session_id, e.timestamp
    """
    grouped: dict[str, list] = {}
    with driver.session() as ses:
        for record in ses.run(query, **params):
            grouped.setdefault(record["session_id"], []).append(dict(record["e"]))
    return list(grouped.items())


def fetch_existing_memories(driver) -> list[dict]:
    with driver.session() as ses:
        result = ses.run("MATCH (m:Memory) RETURN m.path AS path, m.content AS content ORDER BY path")
        return [dict(r) for r in result]


def render_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        head = f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
        if e.get("tool_name"):
            head += f" tool={e['tool_name']}"
        lines.append(head)
        if e.get("prompt"):
            lines.append(f"  prompt: {e['prompt'][:500]}")
        if e.get("tool_input"):
            lines.append(f"  input:  {str(e['tool_input'])[:500]}")
        if e.get("tool_response"):
            lines.append(f"  output: {str(e['tool_response'])[:500]}")
    return "\n".join(lines)


def render_existing(memories: list[dict]) -> str:
    if not memories:
        return "(no existing memories)"
    parts = []
    for m in memories:
        parts.append(f"### {m['path']}\n```\n{m['content']}\n```")
    return "\n\n".join(parts)


def _build_user_msg(transcript: str, existing: str) -> str:
    return (
        f"<existing_memories>\n{existing}\n</existing_memories>\n\n"
        f"<events>\n{transcript}\n</events>"
    )


def _parse_memories(text: str) -> list[dict]:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(text[start : end + 1]).get("memories", [])


def _call_claude_sdk(client, transcript: str, existing: str) -> list[dict]:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": _build_user_msg(transcript, existing)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _parse_memories(text)


def _find_claude_cli() -> str:
    """Return the claude CLI binary path or raise a clear error."""
    explicit = os.environ.get("DREAM_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found is None:
        raise RuntimeError(
            "Neither ANTHROPIC_API_KEY nor a claude CLI binary was found.\n"
            "Install Claude Code (https://claude.ai/code) or set ANTHROPIC_API_KEY."
        )
    return found


def _call_claude_cli(transcript: str, existing: str) -> list[dict]:
    """Call Claude via the claude CLI (supports OAuth / Claude Code subscriptions).

    The user message is fed via stdin to avoid ARG_MAX limits on large transcripts.
    """
    claude_bin = _find_claude_cli()
    result = subprocess.run(
        [
            claude_bin,
            "-p",
            "--tools", "",
            "--model", MODEL,
            "--system-prompt", SYSTEM_PROMPT,
            "--no-session-persistence",
            "--output-format", "text",
        ],
        input=_build_user_msg(transcript, existing),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "(no output)").strip()[:500]
        raise RuntimeError(f"claude CLI exited with code {result.returncode}:\n{detail}")
    return _parse_memories(result.stdout)


def call_claude(client, transcript: str, existing: str) -> list[dict]:
    if client is not None:
        return _call_claude_sdk(client, transcript, existing)
    return _call_claude_cli(transcript, existing)


def chunk_events(events: list[dict], chunk_size: int) -> list[list[dict]]:
    """Split *events* into consecutive windows of at most *chunk_size* items."""
    return [events[i : i + chunk_size] for i in range(0, len(events), chunk_size)]


def dream_chunked(
    client,
    events: list[dict],
    global_existing: str,
    chunk_size: int,
) -> list[dict]:
    """Process a session that exceeds the context window by splitting it into
    sequential chunks of *chunk_size* events.

    Memory accumulation strategy:
    - Chunk 1 receives the global DB memories as context (same as a normal run).
    - Each subsequent chunk receives the memories produced so far by earlier
      chunks.  This lets Claude refine and merge across the full session rather
      than producing independent, potentially duplicate memories per chunk.
    - When two chunks produce a memory at the same path the later chunk wins,
      so the final pass always reflects the most recent evidence.
    """
    chunks = chunk_events(events, chunk_size)
    accumulated: dict[str, dict] = {}  # path → memory, updated after each chunk

    for idx, chunk in enumerate(chunks):
        chunk_num = f"{idx + 1}/{len(chunks)}"
        print(f"  [chunk {chunk_num}] {len(chunk)} events", flush=True)

        existing = (
            global_existing
            if not accumulated
            else render_existing(list(accumulated.values()))
        )
        new_memories = call_claude(client, render_events(chunk), existing)
        for m in new_memories:
            if m.get("path") and m.get("content"):
                accumulated[m["path"]] = m

    return list(accumulated.values())


def write_memories(driver, session_id: str, memories: list[dict], watermark: str) -> int:
    """Upsert memories and advance the session's last_dreamed_at watermark.

    `watermark` is the timestamp of the latest event we just dreamed over —
    future runs will only re-dream the session if newer events arrive.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"path": m["path"], "content": m["content"], "updated_at": now}
        for m in memories
        if m.get("path") and m.get("content")
    ]
    with driver.session() as ses:
        ses.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        ses.run(
            """
            MATCH (s:Session {session_id: $session_id})
            SET s.last_dreamed_at = $watermark
            WITH s
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            SET m.content = row.content, m.updated_at = row.updated_at
            MERGE (s)-[:DREAMED]->(m)
            MERGE (m)-[:DERIVED_FROM]->(s)
            """,
            session_id=session_id,
            watermark=watermark,
            rows=rows,
        )
    return len(rows)


def _resolve_auth(auth_arg: str) -> str:
    """Return the effective auth backend ('sdk' or 'cli').

    Resolution order: --auth flag > DREAM_AUTH env var > 'sdk' default.
    """
    backend = auth_arg or os.environ.get("DREAM_AUTH", "sdk")
    if backend not in ("sdk", "cli"):
        raise SystemExit(f"--auth / DREAM_AUTH must be 'sdk' or 'cli', got {backend!r}")
    return backend


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="dream over a single session_id")
    ap.add_argument("--since", help="only include events newer than e.g. 24h, 7d, 30m")
    ap.add_argument("--dry-run", action="store_true", help="print memories, don't write")
    ap.add_argument(
        "--auth",
        metavar="{sdk,cli}",
        default="",
        help=(
            "auth backend: 'sdk' (ANTHROPIC_API_KEY, default) or "
            "'cli' (claude CLI / OAuth). Overrides DREAM_AUTH env var."
        ),
    )
    ap.add_argument(
        "--max-events",
        type=int,
        default=int(os.environ.get("DREAM_MAX_EVENTS", "0")),
        metavar="N",
        help=(
            "skip sessions with more than N new events (avoids context-window "
            "errors on very large sessions). 0 = no limit (default). "
            "Ignored for sessions processed via --chunk-size. "
            "Also reads DREAM_MAX_EVENTS env var."
        ),
    )
    ap.add_argument(
        "--chunk-size",
        type=int,
        default=int(os.environ.get("DREAM_CHUNK_SIZE", "0")),
        metavar="N",
        help=(
            "split sessions larger than N events into sequential N-event chunks "
            "instead of skipping them. Each chunk receives the memories produced "
            "by previous chunks as context. 0 = disabled (default). "
            "Also reads DREAM_CHUNK_SIZE env var."
        ),
    )
    args = ap.parse_args()

    backend = _resolve_auth(args.auth)
    if backend == "sdk":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set (required for --auth sdk)")
        if _Anthropic is None:
            raise SystemExit("anthropic package is not installed (required for --auth sdk)")
        client = _Anthropic()
    else:
        client = None
        _find_claude_cli()  # validate early before any DB work

    since = parse_since(args.since) if args.since else None
    driver = get_driver()
    try:
        sessions = fetch_events(driver, args.session, since)
        if not sessions:
            print("nothing to dream about.")
            return
        existing = render_existing(fetch_existing_memories(driver))
        for session_id, events in sessions:
            n = len(events)
            use_chunks = args.chunk_size and n > args.chunk_size
            if not use_chunks and args.max_events and n > args.max_events:
                print(
                    f"\n=== skipping {session_id} ({n} events > "
                    f"--max-events {args.max_events}) ==="
                )
                continue
            if use_chunks:
                n_chunks = -(-n // args.chunk_size)  # ceiling division
                print(
                    f"\n=== dreaming over {session_id} "
                    f"({n} events in {n_chunks} chunks of {args.chunk_size}) ==="
                )
            else:
                print(f"\n=== dreaming over {session_id} ({n} new events) ===")
            try:
                if use_chunks:
                    memories = dream_chunked(client, events, existing, args.chunk_size)
                else:
                    memories = call_claude(client, render_events(events), existing)
            except Exception as exc:
                print(f"  ERROR: {exc} — skipping session", file=sys.stderr)
                continue
            for m in memories:
                print(f"\n--- {m.get('path')} ---")
                print(m.get("content", ""))
            if not args.dry_run:
                watermark = events[-1].get("timestamp")
                n = write_memories(driver, session_id, memories, watermark)
                print(f"\n  wrote/updated {n} memories; watermark -> {watermark}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
