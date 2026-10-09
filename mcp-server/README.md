# hiddenjob MCP server

An MCP (Model Context Protocol) server wrapper for the [hiddenjob](../README.md)
auto-apply pipeline. It exposes the local SQLite ledger
(`.hiddenjob/hiddenjob.sqlite3`) as MCP tools so any MCP-capable agent —
Claude Code, ChatGPT, Codex, … — can read pipeline stats, browse ranked roles,
stage roles, and record submissions, with every standing guardrail enforced
server-side.

**Zero runtime dependencies.** Python 3.9+ stdlib only — no `pip install`
needed. It speaks MCP 2024-11-05 over stdio using plain JSON-RPC 2.0.

## Quick start

```bash
# Run directly (stdio transport)
python3 ~/workspace/hiddenjob/mcp-server/server.py

# Point at a different ledger
HIDDENJOB_DB=/path/to/hiddenjob.sqlite3 python3 server.py
# or: python3 server.py --db /path/to/hiddenjob.sqlite3

# Daily submission cap (default 10)
HIDDENJOB_MAX_DAILY_SUBMITS=5 python3 server.py
```

### Claude Code (`npx`-style usage)

Add to `.mcp.json` (project) or `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "hiddenjob": {
      "command": "python3",
      "args": ["/home/hatch/workspace/hiddenjob/mcp-server/server.py"],
      "env": {
        "HIDDENJOB_MAX_DAILY_SUBMITS": "10"
      }
    }
  }
}
```

## Tools

| Tool | Mutates? | What it does |
|---|---|---|
| `pipeline_stats` | No | Stage counts: live jobs by classification, application states, today's submitted count vs the daily cap |
| `list_actionable_roles` | No | Ranked roles using the same qualification gates as the daily cron (Spanish-fluency, intern/new-grad, seniority, location). Returns `score` (ranker points; actionable floor = 20), `role_fit` (title fit; floor = 18), `tier`, `reason` |
| `list_compliance_leads` | No | perm-like / lca-like / recruitment-notice-like postings — the system's core discovery target |
| `get_role_details` | No | Full ledger record + ranker classification for one posting URL |
| `stage_role` | Ledger only | Mark a posting `staged` for review. URL must exist in the ledger |
| `mark_prefilled` | Ledger only | Mark a staged/drafted role `prefilled` after the form is filled (not submitted) |
| `submit_application` | Ledger only | **Record** a submission — see guardrails below |

## Guardrails (enforced server-side, never bypass)

`submit_application` does **not** perform the external application. The calling
agent applies through its own browser/ATS route first, then records the result
here. Recording requires **all** of:

1. `confirm: true` — explicit per-call confirmation, set only after the
   application was actually submitted.
2. Non-empty `evidence` — success-page URL, confirmation text, or receipt
   reference. Nothing is called submitted without confirmation evidence.
3. **Duplicate-sent check** — exact URL and company+title fuzzy match against
   already-submitted records; re-submission is refused.
4. **Spanish-fluency exclusion** — postings matching the bilingual/Spanish-
   required patterns are refused (Barklee is not fluent in Spanish).
5. **Daily submission cap** — default 10/day (`HIDDENJOB_MAX_DAILY_SUBMITS`).

Additional standing rules the calling agent must follow (documented in the
tool schema so the model sees them):

- Truthful answers only — never invent education, experience, dates, or
  credentials.
- Barklee attended Onondaga Community College with **no degree** — answer
  degree questions as "Other / some college, no degree".
- A compliance-lead notice is **not** an open application: resolve it to a
  live employer/ATS application before staging; never apply to the notice
  text itself.
- `stage_role` / `mark_prefilled` / `submit_application` mutate only the
  local ledger. They never touch the network, create accounts, or submit
  anything externally. Existing CLI/cron behavior is unchanged.

## Manual smoke test

```bash
cd ~/workspace/hiddenjob/mcp-server
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"pipeline_stats","arguments":{}}}' \
| python3 server.py 2>server.log
```

Expect: an `initialize` result with `serverInfo.name == "hiddenjob-mcp"`,
a 7-tool list, and a `pipeline_stats` result with real ledger counts.
