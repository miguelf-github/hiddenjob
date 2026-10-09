#!/bin/bash
# Watches Gmail for USAJOBS API key replies (re-issue request of 2026-09-27).
# Catches BOTH the issuance mail (accountinfo@usajobs.gov, "Developer API Key")
# AND help-desk replies (vendor-help@usajobs.gov, ticket thread).
# On a new key: updates ONLY the USAJOBS_API_KEY line in hiddenjob-env (never
# overwrites the file), live-tests it, and reports. Silent when nothing new.
# Migrated 2026-10-01 from Mac-mini SSH gog to the VM-native gmail-throttled
# connector wrapper (Barklee: all Gmail through the normal Gmail connector).
set -uo pipefail
GT_BIN="$HOME/workspace/tools/gmail-throttled/gmail-throttled"
AFTER="2026/09/27"
ENV_FILE="$HOME/.config/hiddenjob-env"
SEEN_FILE="$HOME/.config/usajobs-key-watch-seen"
WATCH_LOG="$HOME/workspace/goals/usajobs-api-key-re-issue/hidden_files/watch-log.txt"

# 2026-10-04: preserve failure evidence. An earlier connector grant flap at
# 04:43 PDT was diagnosed only from a worker paraphrase ("check for expired
# transient grants") because stderr was discarded (2>/dev/null) — the true
# error text was unrecoverable. Fail-loud scripts must log the raw stderr.
log_fail() { # $1 = short label; $2 = detail (pre-truncated by caller)
  printf '%s watch: FAILED — %s | detail: %s\n' "$(date '+%F %T %Z')" "$1" "$2" >> "$WATCH_LOG"
}

search_ids() { # $1 = gmail query; prints message ids, one per line
  local q="$1" out errfile err
  errfile=$(mktemp)
  # Fail-loud: a failed Gmail read must NEVER masquerade as "no results".
  if ! out=$("$GT_BIN" gmail +triage --query "$q" --max 10 --format json 2>"$errfile"); then
    err=$(head -c 500 "$errfile" | tr '\n' ' ')
    rm -f "$errfile"
    echo "READ FAILED: gmail triage query failed (connector/egress issue). Refusing to report NO CHANGE. detail: $err" >&2
    log_fail "triage query failed (q=$q)" "$err"
    return 1
  fi
  err=$(head -c 500 "$errfile" | tr '\n' ' ')
  rm -f "$errfile"
  # The wrapper exits 0 with a '[gmail-throttled] ...' marker on stdout on
  # transport failure — the exit code alone is not a health signal.
  case "$out" in
    "[gmail-throttled]"*)
      echo "READ FAILED: gmail triage transport failure: ${out:0:160}. Refusing to report NO CHANGE. stderr: $err" >&2
      log_fail "triage transport marker (q=$q)" "${out:0:160} | stderr: $err"
      return 1;;
  esac
  # +triage prints empty stdout (not empty JSON) when a query returns zero results.
  [ -z "$out" ] && return 0
  printf '%s' "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m['id'] for m in d.get('messages',[]) if 'id' in m))" 2>/dev/null
}

if ! IDS1=$(search_ids "from:accountinfo@usajobs.gov subject:\"Developer API Key\" after:$AFTER"); then
  echo "READ FAILED: accountinfo search failed (connector/egress issue). Refusing to report NO CHANGE." >&2
  exit 1
fi
if ! IDS2=$(search_ids "from:vendor-help@usajobs.gov after:$AFTER"); then
  echo "READ FAILED: vendor-help search failed (connector/egress issue). Refusing to report NO CHANGE." >&2
  exit 1
fi
IDS=$(printf '%s\n%s\n' "$IDS1" "$IDS2" | sort -u)
[ -z "$IDS" ] && exit 0

SEEN="$(cat "$SEEN_FILE" 2>/dev/null || true)"
NEW=""
for id in $IDS; do
  case "$SEEN" in *"$id"*) ;; *) NEW="$NEW $id";; esac
done
[ -z "$NEW" ] && exit 0

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
UA="${USAJOBS_USER_AGENT:-barkleesanders@gmail.com}"

for id in $NEW; do
  errfile=$(mktemp)
  if ! DETAIL=$("$GT_BIN" gmail +read --id "$id" --headers --format json 2>"$errfile"); then
    err=$(head -c 500 "$errfile" | tr '\n' ' ')
    rm -f "$errfile"
    echo "READ FAILED: could not read message $id (connector/egress issue); leaving it unseen for the next run. detail: $err" >&2
    log_fail "message read failed (id=$id)" "$err"
    exit 1
  fi
  err=$(head -c 500 "$errfile" | tr '\n' ' ')
  rm -f "$errfile"
  case "$DETAIL" in
    "[gmail-throttled]"*)
      echo "READ FAILED: transport failure reading message $id: ${DETAIL:0:160}; leaving it unseen for the next run. stderr: $err" >&2
      log_fail "message read transport marker (id=$id)" "${DETAIL:0:160} | stderr: $err"
      exit 1;;
  esac
  BODY=$(printf '%s' "$DETAIL" | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
t=d.get('body_text') or ''
if not t.strip():
    t=re.sub(r'<[^>]+>',' ',d.get('body_html') or '')
print(t)" 2>/dev/null | tr -d '\r')
  SUBJECT=$(printf '%s' "$DETAIL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('subject',''))" 2>/dev/null | head -c 120)
  # Key formats seen: "Your API key is: <key>" or "API key is\n<key>" (own line)
  KEY=$(printf '%s' "$BODY" | grep -oP '(?:[Yy]our API key is|API key is):?\s*\K[A-Za-z0-9]{16,}' | head -1)
  [ -z "$KEY" ] && KEY=$(printf '%s' "$BODY" | grep -oP '^[A-Za-z0-9]{40,50}$' | head -1)
  if [ -n "$KEY" ]; then
    # Surgical env update: replace the line if present, else append. Never truncate.
    touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    if grep -q '^export USAJOBS_API_KEY=' "$ENV_FILE"; then
      sed -i "s/^export USAJOBS_API_KEY=.*/export USAJOBS_API_KEY='$KEY'/" "$ENV_FILE"
    else
      printf 'export USAJOBS_API_KEY='"'"'%s'"'"'\n' "$KEY" >> "$ENV_FILE"
    fi
    CODE=$(curl -s -m 25 -o /dev/null -w "%{http_code}" -H "User-Agent: $UA" -H "Authorization-Key: $KEY" "https://data.usajobs.gov/api/search?ResultsPerPage=1")
    echo "USAJOBS-NEW-KEY keylen=${#KEY} http=$CODE msgid=$id"
  else
    echo "USAJOBS-NEW-MAIL no-key-found msgid=$id subject=$SUBJECT"
  fi
  printf '%s %s' "$SEEN" "$id" > "$SEEN_FILE"
  SEEN="$SEEN $id"
done
