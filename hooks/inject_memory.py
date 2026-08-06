#!/usr/bin/env python3
"""
Shared hook: inject :Memory nodes into the session as additional context.

- SessionStart: load up to 5 profile/* memories so the model has user context.
- UserPromptSubmit: fulltext search against memory content/path with an OR-term
  fallback when the initial query returns nothing.

Used by both Claude Code and Codex. Both clients accept the same output shape:
  {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}

Requires a fulltext index (create once):
  CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS
  FOR (m:Memory) ON EACH [m.content, m.path]
"""

import argparse
import json
import os
import re
import sys

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MAX_PROMPT_HITS = 5
MIN_FULLTEXT_SCORE = 0.5

STOPWORDS = {
    "this", "that", "with", "from", "have", "what", "when", "where", "which",
    "would", "could", "should", "your", "their", "there", "about", "into",
    "they", "them", "then", "than", "some", "make", "like", "want", "need",
    "just", "only", "also", "still", "very", "much", "more", "most", "ours",
    "please", "thanks", "code", "file", "files",
}


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _fulltext_search(session, query: str, limit: int = MAX_PROMPT_HITS) -> list:
    cypher = """
    CALL db.index.fulltext.queryNodes('memory_fulltext', $query)
    YIELD node, score
    WHERE score > $min_score
    RETURN node.path AS path, node.content AS content, score
    ORDER BY score DESC
    LIMIT $limit
    """
    return list(session.run(cypher, query=query, min_score=MIN_FULLTEXT_SCORE, limit=limit))


def _extract_terms(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", prompt.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def session_start_context() -> str:
    with get_driver() as driver, driver.session() as s:
        profile = list(s.run(
            "MATCH (m:Memory) WHERE m.path STARTS WITH 'profile/' "
            "RETURN m.path AS path, m.content AS content ORDER BY m.path "
            "LIMIT 5"
        ))

    if not profile:
        return ""

    parts = ["# Memory (from prior sessions)\n", "## Profile\n"]
    for r in profile:
        parts.append(f"### {r['path']}\n{r['content']}\n")
    return "\n".join(parts)


def prompt_context(prompt: str) -> str:
    if not prompt.strip():
        return ""

    with get_driver() as driver, driver.session() as s:
        rows = _fulltext_search(s, prompt)

        if not rows:
            terms = _extract_terms(prompt)
            if terms:
                lucene_query = " OR ".join(terms)
                rows = _fulltext_search(s, lucene_query)

    if not rows:
        return ""

    parts = ["# Relevant memory for this prompt\n"]
    for r in rows:
        parts.append(f"## {r['path']}\n{r['content']}\n")
    return "\n".join(parts)


def emit(event_name: str, context: str):
    if not context.strip():
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }
    print(json.dumps(out))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, choices=["claude_code", "codex", "cursor", "opencode"])
    parser.parse_args()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        event = data.get("hook_event_name")
        normalized = (event or "").lower()
        if normalized in {"sessionstart", "session_start"}:
            emit(event or "sessionStart", session_start_context())
        elif normalized in {"userpromptsubmit", "beforesubmitprompt"}:
            emit(event or "beforeSubmitPrompt", prompt_context(data.get("prompt", "")))
    except Exception as e:
        print(f"inject_memory error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
