#!/bin/sh
# halt-check.sh: read the fleet-wide halt flag before any unattended write.
#
# Canonical copy: pete-builds/fleet-workflows halt-check/halt-check.sh.
# Every other copy must stay byte-identical (the weekly halt drill compares sha256).
#
# Contract (prints exactly one line):
#   exit 0  "halt-check: clear"              the switch repo answers and HALT is absent
#   exit 3  "halt-check: HALTED: <reason>"   HALT exists; its first line is the reason
#   exit 4  "halt-check: UNKNOWN: <detail>"  anything else; unattended writers must skip
#
# A 404 for HALT only means "clear" when the ALIVE marker in the same repo answers 200.
# Without that, a renamed, deleted or private repo would 404 too and read as clear.
#
# Overrides, for drills only:
#   FLEET_HALT_URL        replaces the HALT URL
#   FLEET_HALT_ALIVE_URL  replaces the ALIVE marker URL

base="https://raw.githubusercontent.com/pete-builds/fleet-workflows/main"
halt_url="${FLEET_HALT_URL:-$base/HALT}"
alive_url="${FLEET_HALT_ALIVE_URL:-$base/halt-check/ALIVE}"

if ! command -v curl >/dev/null 2>&1; then
  echo "halt-check: UNKNOWN: curl not found"
  exit 4
fi

tmp="$(mktemp 2>/dev/null)" || tmp="/tmp/halt-check.$$"
trap 'rm -f "$tmp"' EXIT

code="$(curl -sS --max-time 10 -o "$tmp" -w '%{http_code}' "$halt_url" 2>/dev/null)"
rc=$?

case "$code" in
  200)
    reason="$(head -n 1 "$tmp" | tr -d '\r' | cut -c1-200)"
    [ -n "$reason" ] || reason="(no reason given)"
    echo "halt-check: HALTED: $reason"
    exit 3
    ;;
  404)
    alive="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$alive_url" 2>/dev/null)"
    if [ "$alive" = "200" ]; then
      echo "halt-check: clear"
      exit 0
    fi
    echo "halt-check: UNKNOWN: HALT absent but ALIVE marker answered HTTP ${alive:-none} at $alive_url"
    exit 4
    ;;
  *)
    echo "halt-check: UNKNOWN: curl exit $rc, HTTP ${code:-none} at $halt_url"
    exit 4
    ;;
esac
