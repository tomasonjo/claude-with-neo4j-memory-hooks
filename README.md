Deprecated: see https://github.com/neo4j-labs/meta-knowledge-graph

# Agent Memory Hooks (Claude Code + Codex + Cursor)

A two-stage memory system for [Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex/hooks), and
[Cursor](https://www.cursor.com/), backed by Neo4j.

1. **Hooks (online)** — capture every session event from either agent into a
   shared graph as it happens.
2. **Dream phase (offline)** — periodically read those events and distill
   them into durable, markdown-style memories that future sessions can use.

The hooks record *what happened*. The dream phase decides *what's worth
remembering*. Both clients write into the same Neo4j instance; nodes are
tagged with `client: "claude_code" | "codex" | "cursor"` so you can query
across or within agents.

## Repo layout

```
hooks/
  log_event.py             # shared writer (takes --client)
  inject_memory.py         # shared memory injector
.claude/
  settings.json            # registers hooks with Claude Code
  hooks/
    log_event.sh           # → hooks/log_event.py --client claude_code
    inject_memory.sh       # → hooks/inject_memory.py --client claude_code
.codex/
  hooks.json               # registers hooks with Codex
  hooks/
    log_event.sh           # → hooks/log_event.py --client codex
    inject_memory.sh       # → hooks/inject_memory.py --client codex
.cursor/
  hooks.json               # modern Cursor hook registration
  settings.json            # legacy/compat Cursor hook registration
  hooks/
    log_event.sh           # → hooks/log_event.py --client cursor
    inject_memory.sh       # → hooks/inject_memory.py --client cursor
dream/
  dream.py                 # offline consolidation script
  README.md                # dream-phase docs
  requirements.txt
requirements.txt           # hook deps (just neo4j driver)
test_hooks.py              # smoke test for the hook writer
```

## Stage 1 — Hooks

Each session (Claude Code, Codex, or Cursor) becomes a linked list of events:

```
(Session {session_id, client}) -[:FIRST_EVENT]->  (Event) -[:NEXT]-> (Event) -> ...
                               -[:LATEST_EVENT]-> (last Event)
```

Events captured: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `Stop`. Codex also exposes `PermissionRequest` (not currently
wired up). Each `:Event` stores the raw hook payload — prompt, tool name,
tool input, tool response, transcript snapshot, plus Codex-specific
`turn_id` / `tool_use_id` / `last_assistant_message` when present.

### Setup

```bash
pip install -r requirements.txt
# defaults assume bolt://localhost:7687 with neo4j/password
export HOOKS_NEO4J_URI=bolt://localhost:7687
export HOOKS_NEO4J_USER=neo4j
export HOOKS_NEO4J_PASSWORD=password
```

The hooks are already wired up:
- **Claude Code**: `.claude/settings.json` — run Claude Code from this dir.
- **Codex**: `.codex/hooks.json` — run Codex from this dir with
  `[features] codex_hooks = true` enabled in `~/.codex/config.toml`.
- **Cursor**: `.cursor/hooks.json` (preferred) or `.cursor/settings.json`
  (legacy compatibility) — open this dir in Cursor.

For Python dependencies, Cursor wrappers prefer `./.venv/bin/python` and
fallback to `python3`. This avoids interpreter mismatches when Cursor runs in
a different environment than your shell.

Both clients stream events into the same Neo4j instance; Session/Event nodes
are tagged with the originating `client`.

### Test

```bash
python test_hooks.py    # requires a running Neo4j
```

## Stage 2 — Dream phase

Reads sessions that have events newer than their `last_dreamed_at`
watermark, asks Claude to extract durable memories, and upserts them as
`:Memory` nodes whose `path` + `content` imitate a markdown file.

```bash
pip install -r dream/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python dream/dream.py              # all sessions with new events
python dream/dream.py --since 24h  # only events from last 24h
python dream/dream.py --dry-run    # preview without writing
```

Memory paths are organized semantically:

```
profile/role.md
profile/preferences.md
tools/bash/common-flags.md
project/<slug>.md
general/<slug>.md
```

See [dream/README.md](dream/README.md) for full docs (schema, re-run
behavior, inspect/reset queries).

## Full graph schema

```
(:Session {session_id, client, created_at, last_dreamed_at})
  -[:FIRST_EVENT]->  (:Event)
  -[:LATEST_EVENT]-> (:Event)
  -[:DREAMED]->      (:Memory)

(:Event {event_id, event_name, client, timestamp, tool_name, tool_input,
         tool_use_id, tool_response, prompt, model, source, turn_id,
         last_assistant_message, stop_hook_active, transcript_path,
         transcript, cwd})
  -[:NEXT]-> (:Event)

(:Memory {path, content, updated_at})              // path unique
  -[:DERIVED_FROM]-> (:Session)
```

## Suggested workflow

1. Use Claude Code as normal — hooks capture everything.
2. Run `python dream/dream.py` on a cadence that suits you (manually,
   nightly cron, or after each session).
3. Future sessions / agents can read `:Memory` nodes by path to get a fast
   profile of who the user is, what tools work well, and what's going on
   in the project.
