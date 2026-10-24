#!/bin/bash
# Exclusive build lock: an atomic mkdir holding the owner pid.
#   lock.sh acquire|release --lock-dir DIR --pid PID
# Exit codes: 0 ok, 2 usage, 3 held/refused.
# A lock whose owner is dead is reported as stale and refused, never taken over: two runs that both saw the
# dead pid could otherwise both win. Recover by hand only after proving the recorded pid is dead
# (`kill -0 <pid>` fails) and that no build process (gradlew, expo prebuild, build-apk.sh) is running for this
# source; then remove the lock directory.
set -uo pipefail

usage() { echo "usage: $(basename "$0") acquire|release --lock-dir DIR --pid PID" >&2; exit 2; }

[ $# -ge 1 ] || usage
ACTION=$1; shift
LOCK_DIR=""; OWNER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lock-dir) [ $# -ge 2 ] || usage; LOCK_DIR=$2; shift 2 ;;
    --pid) [ $# -ge 2 ] || usage; OWNER=$2; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$LOCK_DIR" ] && [ -n "$OWNER" ] || usage
case "$OWNER" in *[!0-9]*|"") usage ;; esac

holder() { tr -d ' \n' < "$LOCK_DIR/pid" 2>/dev/null || true; }
alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

case "$ACTION" in
  acquire)
    mkdir -p "$(dirname "$LOCK_DIR")" || exit 3
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      tmp="$LOCK_DIR/pid.tmp.$OWNER"
      echo "$OWNER" > "$tmp" && mv -f "$tmp" "$LOCK_DIR/pid" || { rm -rf "$LOCK_DIR"; exit 3; }
      echo "lock acquired: $LOCK_DIR (pid $OWNER)"
      exit 0
    fi
    pid=$(holder)
    if [ -z "$pid" ]; then
      echo "lock $LOCK_DIR is being created or has no owner pid; refusing" >&2
    elif alive "$pid"; then
      echo "lock $LOCK_DIR is held by live pid $pid; refusing" >&2
    else
      echo "stale lock $LOCK_DIR: owner pid $pid is dead; refusing. Remove it after checking no build runs." >&2
    fi
    exit 3 ;;
  release)
    pid=$(holder)
    if [ "$pid" != "$OWNER" ]; then
      echo "lock $LOCK_DIR is owned by pid ${pid:-none}, not $OWNER; kept" >&2
      exit 3
    fi
    rm -rf "$LOCK_DIR"
    exit 0 ;;
  *) usage ;;
esac
