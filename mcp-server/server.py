#!/usr/bin/env python3
"""hiddenjob MCP server — stdio transport, zero runtime dependencies.

Exposes Barklee's hiddenjob auto-apply pipeline (~/workspace/hiddenjob) as
Model Context Protocol tools so any MCP-capable agent (Claude Code, ChatGPT,
Codex, …) can drive it: read pipeline stats, list ranked roles, stage roles,
and record submissions — with every standing guardrail enforced server-side.

Read-mostly safe by design:
  - pipeline_stats / list_actionable_roles / list_compliance_leads /
    get_role_details never mutate anything.
  - stage_role / mark_prefilled mutate only the local SQLite ledger
    (.hiddenjob/hiddenjob.sqlite3); they never touch the network, never
    create accounts, never submit anything externally.
  - submit_application does NOT perform an external submission. It records a
    'submitted' state in the local ledger ONLY after all standing gates pass:
    explicit per-call confirm=true, non-empty confirmation evidence, duplicate
    checks, the Spanish-fluency exclusion, and the daily submission cap. The
    calling agent performs the actual application through its own browser/ATS
    route and supplies the evidence (success-page URL / confirmation text).

Standing rules enforced here (never bypass):
  - Truthful answers only — the server never invents education, experience,
    dates, or credentials. (Enforced on the calling agent via tool docs.)
  - Never apply to bilingual-Spanish-required roles (server-side pattern gate).
  - Duplicate-sent check before every submission (exact URL + company/title).
  - Confirmation evidence required before anything is called submitted.
  - Daily submission cap (default 10/day, HIDDENJOB_MAX_DAILY_SUBMITS).

Protocol: JSON-RPC 2.0 over stdio, newline-delimited (MCP 2024-11-05).
No third-party packages — stdlib only.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

SERVER_NAME = "hiddenjob-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

HIDDENJOB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = HIDDENJOB_ROOT / ".hiddenjob" / "hiddenjob.sqlite3"

MAX_DAILY_SUBMITS = int(os.environ.get("HIDDENJOB_MAX_DAILY_SUBMITS", "10"))

# Ranker reuse: same qualification gates the daily cron uses.
sys.path.insert(0, str(HIDDENJOB_ROOT))
from tools.hiddenjob_rank import (  # noqa: E402
    HARD_EXCLUDE,
    NOTICE_CLASSIFICATIONS,
    classify,
)

# The ranker lists its Spanish-fluency patterns first in HARD_EXCLUDE
# (marked "Spanish fluency (Barklee is not fluent — 2026-09-25)").
SPANISH_PATTERNS = HARD_EXCLUDE[:6]

VALID_PREFILL_FROM = {"staged", "drafted"}
VALID_SUBMIT_FROM = {"staged", "drafted", "prefilled"}


def log(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def resolve_db(cli_db: str | None) -> Path:
    if cli_db:
        return Path(cli_db).expanduser()
    env = os.environ.get("HIDDENJOB_DB")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def db_connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"ledger not found: {db}")
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def today_submitted_count(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COUNT(*) AS n FROM applications "
        "WHERE state='submitted' AND substr(updated_at,1,10)=substr(datetime('now'),1,10)"
    ).fetchone()
    return int(row["n"])


def load_description(evidence_path: str | None) -> tuple[str, str]:
    """Mirror the ranker's evidence read: description text + location context."""
    if not evidence_path:
        return "", ""
    p = Path(evidence_path)
    if p.suffix != ".json" or not p.exists():
        return "", ""
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return "", ""
    desc = json.dumps(raw)[:8000]
    loc_ctx = ""
    if isinstance(raw, dict):
        bits = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    lk = str(k).lower()
                    if any(t in lk for t in ("location", "address", "workplace", "office")):
                        bits.append(json.dumps(v)[:400])
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(raw)
        loc_ctx = " ".join(bits).lower()
    return desc, loc_ctx


def ranked_live_jobs(con: sqlite3.Connection):
    """Score every live job with the ranker's classify(); return sorted list."""
    rows = con.execute(
        "SELECT url, company, title, classification, evidence_path, date_posted, source "
        "FROM jobs WHERE live=1"
    ).fetchall()
    out = []
    for r in rows:
        desc, loc_ctx = load_description(r["evidence_path"])
        c = classify(r["title"], r["company"], desc, loc_ctx, r["classification"])
        app = con.execute(
            "SELECT state, updated_at FROM applications WHERE job_url=?", (r["url"],)
        ).fetchone()
        out.append(
            {
                "tier": c["tier"],
                "score": c["score"],
                "role_fit": c["role_fit"],
                "reason": c["reason"],
                "url": r["url"],
                "company": r["company"],
                "title": r["title"],
                "source": r["source"],
                "date_posted": r["date_posted"],
                "classification": r["classification"],
                "application_state": app["state"] if app else None,
                "application_updated_at": app["updated_at"] if app else None,
            }
        )
    order = {"actionable": 0, "compliance-lead": 1, "watch": 2, "excluded": 3}
    out.sort(key=lambda x: (order[x["tier"]], -x["score"]))
    return out


def job_row(con: sqlite3.Connection, url: str):
    return con.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()


def app_row(con: sqlite3.Connection, url: str):
    return con.execute("SELECT * FROM applications WHERE job_url=?", (url,)).fetchone()


def spanish_block(title: str, description: str) -> str | None:
    """Return the matching Spanish pattern, or None. Standing rule: never apply
    to bilingual-Spanish-required roles (Barklee is not fluent)."""
    text = f"{title} {description or ''}".lower()[:8000]
    text = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", text)
    for pat in SPANISH_PATTERNS:
        if re.search(pat, text):
            return pat
    return None


def duplicate_submitted(con: sqlite3.Connection, url: str, company: str, title: str):
    """Exact-URL and company+title duplicate checks against submitted records."""
    row = con.execute(
        "SELECT job_url FROM applications WHERE job_url=? AND state='submitted'", (url,)
    ).fetchone()
    if row:
        return {"duplicate": True, "match_type": "exact_url", "job_url": row["job_url"]}
    ct, tt = (company or "").lower(), (title or "").lower()
    if ct and tt:
        rows = con.execute(
            "SELECT a.job_url, j.title FROM applications a JOIN jobs j ON j.url=a.job_url "
            "WHERE a.state='submitted' AND LOWER(j.company)=?",
            (ct,),
        ).fetchall()
        for r in rows:
            rt = (r["title"] or "").lower()
            if tt in rt or rt in tt:
                return {
                    "duplicate": True,
                    "match_type": "company_title",
                    "job_url": r["job_url"],
                    "matched_title": r["title"],
                }
    return {"duplicate": False}


# ---------------------------------------------------------------- tools

def tool_pipeline_stats(con: sqlite3.Connection, _args: dict) -> dict:
    by_class = [
        {"classification": r[0], "count": r[1]}
        for r in con.execute(
            "SELECT classification, COUNT(*) FROM jobs WHERE live=1 "
            "GROUP BY classification ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]
    app_states = [
        {"state": r[0], "count": r[1]}
        for r in con.execute(
            "SELECT state, COUNT(*) FROM applications GROUP BY state ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]
    submitted_today = today_submitted_count(con)
    return {
        "ledger_jobs_total": con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "jobs_live": con.execute("SELECT COUNT(*) FROM jobs WHERE live=1").fetchone()[0],
        "jobs_by_classification": by_class,
        "application_states": app_states,
        "submitted_today": submitted_today,
        "daily_cap": MAX_DAILY_SUBMITS,
        "daily_remaining": max(0, MAX_DAILY_SUBMITS - submitted_today),
    }


def tool_list_actionable_roles(con: sqlite3.Connection, args: dict) -> dict:
    limit = max(1, min(int(args.get("limit", 20)), 100))
    tiers = args.get("tiers", ["actionable"])
    if isinstance(tiers, str):
        tiers = [tiers]
    ranked = ranked_live_jobs(con)
    picked = [r for r in ranked if r["tier"] in tiers][:limit]
    return {
        "count": len(picked),
        "tiers": tiers,
        # score: ranker points (ACTIONABLE floor = 20; higher = stronger fit).
        # role_fit: title-to-role-family points (floor = 18 for actionable).
        "roles": picked,
    }


def tool_list_compliance_leads(con: sqlite3.Connection, args: dict) -> dict:
    limit = max(1, min(int(args.get("limit", 20)), 100))
    ranked = ranked_live_jobs(con)
    picked = [r for r in ranked if r["tier"] == "compliance-lead"][:limit]
    return {
        "count": len(picked),
        "notice": (
            "A compliance-lead notice is NOT an open application by itself. "
            "Resolve it to a live employer/ATS application before staging; "
            "never apply to the notice text itself."
        ),
        "leads": picked,
    }


def tool_get_role_details(con: sqlite3.Connection, args: dict) -> dict:
    url = args.get("url", "")
    if not url:
        raise ValueError("url is required")
    job = job_row(con, url)
    if not job:
        raise ValueError("URL is not in the local evidence ledger")
    desc, loc_ctx = load_description(job["evidence_path"])
    c = classify(job["title"], job["company"], desc, loc_ctx, job["classification"])
    app = app_row(con, url)
    return {
        "job": dict(job),
        "classification_result": c,
        "application": dict(app) if app else None,
        "description_chars": len(desc),
    }


def tool_stage_role(con: sqlite3.Connection, args: dict) -> dict:
    url = args.get("url", "")
    note = args.get("note", "") or ""
    if not url:
        raise ValueError("url is required")
    if not job_row(con, url):
        raise ValueError("That URL is not in the local evidence ledger. Sync and review the posting first.")
    con.execute(
        "INSERT INTO applications(job_url,state,note,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(job_url) DO UPDATE SET state=excluded.state,note=excluded.note,"
        "updated_at=excluded.updated_at",
        (url, "staged", note, utcnow()),
    )
    con.commit()
    return {"ok": True, "url": url, "state": "staged",
            "notice": "Staged for review. No application was submitted or sent."}


def tool_mark_prefilled(con: sqlite3.Connection, args: dict) -> dict:
    url = args.get("url", "")
    note = args.get("note", "") or ""
    if not url:
        raise ValueError("url is required")
    app = app_row(con, url)
    if not app:
        raise ValueError("Stage the job first so the record has a reviewed source posting.")
    if app["state"] not in VALID_PREFILL_FROM:
        raise ValueError(
            f"Cannot mark prefilled from state '{app['state']}'; must be one of {sorted(VALID_PREFILL_FROM)}."
        )
    con.execute(
        "UPDATE applications SET state=?,note=?,updated_at=? WHERE job_url=?",
        ("prefilled", note, utcnow(), url),
    )
    con.commit()
    return {"ok": True, "url": url, "state": "prefilled",
            "notice": "Marked prefilled. Review before submitting."}


def tool_submit_application(con: sqlite3.Connection, args: dict) -> dict:
    """Record a submission in the ledger. Every standing gate is checked first.

    This tool does NOT perform the external submission — the calling agent does
    that through its own browser/ATS route and supplies confirmation evidence.
    """
    url = args.get("url", "")
    confirm = args.get("confirm", False)
    evidence = (args.get("evidence", "") or "").strip()
    note = args.get("note", "") or ""
    if not url:
        raise ValueError("url is required")
    # Gate 1: explicit per-call confirmation flag.
    if confirm is not True:
        raise ValueError(
            "Submission refused: explicit per-call confirmation required "
            "(pass confirm=true after you have actually submitted)."
        )
    # Gate 2: confirmation evidence required before anything is called submitted.
    if not evidence:
        raise ValueError(
            "Submission refused: confirmation evidence is required "
            "(success-page URL, confirmation text, or receipt reference)."
        )
    job = job_row(con, url)
    if not job:
        raise ValueError("That URL is not in the local evidence ledger.")
    app = app_row(con, url)
    if not app:
        raise ValueError("Stage and prefill the job before recording a submission.")
    if app["state"] not in VALID_SUBMIT_FROM:
        raise ValueError(
            f"Cannot record submission from state '{app['state']}'; "
            f"must be one of {sorted(VALID_SUBMIT_FROM)}."
        )
    # Gate 3: duplicate-sent check (exact URL + company/title fuzzy).
    dup = duplicate_submitted(con, url, job["company"], job["title"])
    if dup["duplicate"]:
        raise ValueError(
            f"Submission refused: duplicate of already-submitted record "
            f"({dup['match_type']}: {dup['job_url']})."
        )
    # Gate 4: Spanish-fluency exclusion (Barklee is not fluent).
    desc, _ = load_description(job["evidence_path"])
    hit = spanish_block(job["title"], desc)
    if hit:
        raise ValueError(
            "Submission refused: posting matches the Spanish-fluency exclusion "
            f"(pattern: {hit}). Never apply to bilingual-Spanish-required roles."
        )
    # Gate 5: daily submission cap.
    submitted_today = today_submitted_count(con)
    if submitted_today >= MAX_DAILY_SUBMITS:
        raise ValueError(
            f"Submission refused: daily cap reached ({submitted_today}/{MAX_DAILY_SUBMITS})."
        )
    full_note = f"{note} | evidence: {evidence}".strip(" |")
    con.execute(
        "UPDATE applications SET state='submitted',note=?,updated_at=? WHERE job_url=?",
        (full_note, utcnow(), url),
    )
    con.commit()
    return {
        "ok": True,
        "url": url,
        "state": "submitted",
        "submitted_today": submitted_today + 1,
        "daily_cap": MAX_DAILY_SUBMITS,
    }


TOOLS = [
    {
        "name": "pipeline_stats",
        "description": (
            "Read-only. Pipeline stage counts for the hiddenjob ledger: live jobs "
            "by classification, application states, and today's submitted count "
            "against the daily cap."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_pipeline_stats,
    },
    {
        "name": "list_actionable_roles",
        "description": (
            "Read-only. Rank live ledger postings with the same qualification gates "
            "the daily cron uses (Spanish-fluency, intern/new-grad, seniority, "
            "location). Returns score, role_fit, tier, and reason per role. "
            "score is ranker points (actionable floor = 20; higher = stronger fit). "
            "Tiers: actionable, compliance-lead, watch, excluded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "tiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["actionable"],
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_list_actionable_roles,
    },
    {
        "name": "list_compliance_leads",
        "description": (
            "Read-only. List compliance-lead postings (perm-like / lca-like / "
            "recruitment-notice-like): the system's core discovery target. A notice "
            "is NOT an open application — resolve it to a live employer/ATS "
            "application before staging; never apply to the notice text itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}
            },
            "additionalProperties": False,
        },
        "handler": tool_list_compliance_leads,
    },
    {
        "name": "get_role_details",
        "description": (
            "Read-only. Full ledger record for one posting URL: job fields, the "
            "ranker classification result, and any application record. Use before "
            "staging to verify the role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_get_role_details,
    },
    {
        "name": "stage_role",
        "description": (
            "Mutates the local ledger only (no network, no submission). Mark a "
            "ledger posting as staged for review. The URL must already exist in "
            "the evidence ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_stage_role,
    },
    {
        "name": "mark_prefilled",
        "description": (
            "Mutates the local ledger only. Mark a staged/drafted role as "
            "prefilled after the application form has been filled (not submitted). "
            "Review is still required before any submission."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_mark_prefilled,
    },
    {
        "name": "submit_application",
        "description": (
            "RECORDS a submission in the local ledger; it does NOT perform the "
            "external application. The calling agent must actually apply through "
            "its own browser/ATS route first, then call this with confirmation "
            "evidence. Standing rules enforced server-side, never bypass: "
            "(1) confirm=true is required per call; "
            "(2) non-empty confirmation evidence is required (success-page URL, "
            "confirmation text, or receipt reference) — nothing is called "
            "submitted without it; "
            "(3) duplicate-sent check (exact URL and company+title) refuses "
            "re-submission; "
            "(4) bilingual-Spanish-required roles are refused (Barklee is not "
            "fluent); "
            "(5) daily submission cap enforced (default 10/day). "
            "All answers submitted externally must be truthful: never invent "
            "education, experience, dates, or credentials. Barklee attended "
            "Onondaga Community College with NO degree — answer degree questions "
            "as 'Other / some college, no degree'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true ONLY after you have actually submitted the application.",
                },
                "evidence": {
                    "type": "string",
                    "default": "",
                    "description": "Confirmation evidence: success-page URL, confirmation text, or receipt reference.",
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_submit_application,
    },
]


def tool_schemas():
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOLS
    ]


def call_tool(name: str, args: dict, db: Path) -> dict:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise ValueError(f"unknown tool: {name}")
    con = db_connect(db)
    try:
        return tool["handler"](con, args or {})
    finally:
        con.close()


# ---------------------------------------------------------------- JSON-RPC

def respond(msg_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg: dict, db: Path):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": tool_schemas()}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args, db)
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        except (ValueError, FileNotFoundError) as e:
            return {
                "content": [{"type": "text", "text": f"ERROR: {e}"}],
                "isError": True,
            }
        except Exception:
            log("unhandled tool error:\n" + traceback.format_exc())
            return {
                "content": [{"type": "text", "text": "ERROR: internal server error"}],
                "isError": True,
            }
    if method == "ping":
        return {}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    raise ValueError(f"method not found: {method}")


def serve(db: Path) -> None:
    log(f"starting {SERVER_NAME} v{SERVER_VERSION}, db={db}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad JSON: {e}")
            continue
        msg_id = msg.get("id")
        try:
            result = handle(msg, db)
        except ValueError as e:
            if msg_id is not None:
                respond(msg_id, error={"code": -32601, "message": str(e)})
            continue
        except Exception:
            log("unhandled error:\n" + traceback.format_exc())
            if msg_id is not None:
                respond(msg_id, error={"code": -32603, "message": "internal error"})
            continue
        # Notifications (no id) get no response.
        if msg_id is None:
            continue
        respond(msg_id, result=result if result is not None else {})


def main() -> None:
    ap = argparse.ArgumentParser(description="hiddenjob MCP server (stdio)")
    ap.add_argument("--db", default=None, help="SQLite ledger path (default: .hiddenjob/hiddenjob.sqlite3; or HIDDENJOB_DB)")
    args = ap.parse_args()
    db = resolve_db(args.db)
    serve(db)


if __name__ == "__main__":
    main()
