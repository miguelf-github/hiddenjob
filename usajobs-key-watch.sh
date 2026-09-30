#!/bin/bash
# Watches Gmail for USAJOBS API key replies (re-issue request of 2026-09-27).
# Catches BOTH the issuance mail (accountinfo@usajobs.gov, "Developer API Key")
# AND help-desk replies (vendor-help@usajobs.gov, ticket thread).
# On a new key: updates ONLY the USAJOBS_API_KEY line in hiddenjob-env (never
# overwrites the file), live-tests it, and reports. Silent when nothing new.
set -u
SSH="ssh -F /home/hatch/.ssh/mini_ssh_config mini"
GOG="~/tools/gog-throttled/gog-throttled -a barkleesanders@gmail.com gmail"
AFTER="2026/09/27"
ENV_FILE="$HOME/.config/hiddenjob-env"
SEEN_FILE="$HOME/.config/usajobs-key-watch-seen"

search_ids() { # $1 = gmail query; prints message ids, one per line
  $SSH "$GOG search '$1' --max 10 2>/dev/null" | awk 'NR>1 && $1 ~ /^[0-9a-f]+$/ {print $1}'
}

IDS=$( { search_ids "from:accountinfo@usajobs.gov subject:\"Developer API Key\" after:$AFTER"; search_ids "from:vendor-help@usajobs.gov after:$AFTER"; } | sort -u )
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
  BODY=$($SSH "$GOG read $id 2>/dev/null" | tr -d '\r')
  # Key formats seen: "Your API key is: <key>" or "API key is\n<key>" (own line)
  KEY=$(printf '%s' "$BODY" | grep -oP '(?:[Yy]our API key is|API key is):?\s*\K[A-Za-z0-9]{16,}' | head -1)
  [ -z "$KEY" ] && KEY=$(printf '%s' "$BODY" | grep -oP '^[A-Za-z0-9]{40,50}$' | head -1)
  if [ -n "$KEY" ]; then
    # Surgical env update: replace the line if present, else append. Never truncate.
    touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    if grep -q '^export USAJOBS_API_KEY=' "$ENV_FILE"; then
      sed -i "s/^export USAJOBS_API_KEY=.*/export USAJOBS_API_KEY=$KEY/" "$ENV_FILE"
    else
      printf 'export USAJOBS_API_KEY=%s\n' "$KEY" >> "$ENV_FILE"
    fi
    CODE=$(curl -s -m 25 -o /dev/null -w "%{http_code}" -H "User-Agent: $UA" -H "Authorization-Key: $KEY" "https://data.usajobs.gov/api/search?ResultsPerPage=1")
    echo "USAJOBS-NEW-KEY keylen=${#KEY} http=$CODE msgid=$id"
  else
    SUBJECT=$(printf '%s' "$BODY" | grep -m1 '^Subject:' | cut -c10- | head -c 120)
    echo "USAJOBS-NEW-MAIL no-key-found msgid=$id subject=$SUBJECT"
  fi
  printf '%s %s' "$SEEN" "$id" > "$SEEN_FILE"
  SEEN="$SEEN $id"
done
