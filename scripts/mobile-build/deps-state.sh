#!/bin/bash
# Dependency install state for one source checkout.
#   deps-state.sh check|install|fingerprint --src DIR --state-dir DIR
#   deps-state.sh adopt --src DIR --state-dir DIR --receipt FILE
# check:   0 hit, 1 miss, 3 precondition. Never installs. A miss is: no deps.ok, a changed fingerprint input,
#          a missing node_modules directory, or a missing or changed node_modules/.package-lock.json.
# install: hit -> 0 without npm ci. Miss -> removes <state-dir>/deps.ok, runs `npm ci` in --src, and writes
#          deps.ok atomically only after npm exits 0 and the install is substantiated. Failure -> 4, no marker.
# adopt:   writes deps.ok from a recorded successful install receipt (JSON), only when all 17 checks pass:
#            status == "success", exit == 0, command starts with "npm ci", realpath(worktree) == realpath(--src),
#            lockSha256Before == lockSha256After == sha256(package-lock.json),
#            installedLockSha256 == sha256(node_modules/.package-lock.json),
#            workspacePackageJsonsSha256, patchesDirSha256, postinstallScriptSha256, node, npm, platform and arch
#            equal this script's fingerprint, and postconditions.{nodeModulesRealDir, hiddenLockPresent,
#            lockUnchanged} are all true. Any mismatch -> 4 and the marker is not touched. The checks run even when
#            deps.ok already matches; a valid receipt then leaves deps.ok unchanged.
# A symlinked node_modules is refused (3) and never written through.
set -uo pipefail

[ -z "${PASEO_BUILD_BIN_DIR:-}" ] || PATH="$PASEO_BUILD_BIN_DIR:$PATH"

usage() { echo "usage: $(basename "$0") check|install|fingerprint|adopt --src DIR --state-dir DIR [--receipt FILE]" >&2; exit 2; }

[ $# -ge 1 ] || usage
ACTION=$1; shift
SRC=""; STATE=""; RECEIPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --src) [ $# -ge 2 ] || usage; SRC=$2; shift 2 ;;
    --state-dir) [ $# -ge 2 ] || usage; STATE=$2; shift 2 ;;
    --receipt) [ $# -ge 2 ] || usage; RECEIPT=$2; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$SRC" ] && [ -n "$STATE" ] || usage
[ -d "$SRC" ] || { echo "source $SRC not found" >&2; exit 3; }
SRC=$(cd "$SRC" && pwd -P)
MARKER="$STATE/deps.ok"
NM="$SRC/node_modules"

sha() { if [ -f "$1" ]; then shasum -a 256 "$1" | cut -d' ' -f1; else echo missing; fi; }

# The same computations as the recorded S1 receipt, so a receipt can be compared field by field.
lock_sha() { sha "$SRC/package-lock.json"; }
installed_lock_sha() { sha "$NM/.package-lock.json"; }
workspace_sha() { (cd "$SRC" && cat package.json packages/*/package.json 2>/dev/null | shasum -a 256 | cut -d' ' -f1); }
patches_sha() {
  [ -d "$SRC/patches" ] || { echo none; return; }
  (cd "$SRC" && find patches -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
}
postinstall_sha() { sha "$SRC/scripts/postinstall-patches.mjs"; }

fingerprint() {
  echo "schema=paseo-mobile-build-deps-v1"
  echo "lock=$(lock_sha)"
  echo "workspace_package_jsons=$(workspace_sha)"
  echo "patches=$(patches_sha)"
  echo "postinstall=$(postinstall_sha)"
  echo "node=$(node -v 2>/dev/null)"
  echo "npm=$(npm -v 2>/dev/null)"
  echo "platform=$(uname -s | tr '[:upper:]' '[:lower:]')"
  echo "arch=$(uname -m)"
}

refuse_symlink() {
  if [ -L "$NM" ]; then
    echo "node_modules in $SRC is a symlink; refusing (no shared or foreign installs)" >&2
    exit 3
  fi
}

write_marker() {  # stdin -> deps.ok, atomically
  mkdir -p "$STATE" || return 1
  local tmp="$MARKER.tmp.$$"
  cat > "$tmp" && mv -f "$tmp" "$MARKER"
}

# 0 hit, 1 miss; prints the reason.
is_hit() {
  [ -f "$MARKER" ] || { echo "miss: no deps.ok"; return 1; }
  local recorded current
  recorded=$(grep -E '^(schema|lock|workspace_package_jsons|patches|postinstall|node|npm|platform|arch)=' "$MARKER")
  current=$(fingerprint)
  if [ "$recorded" != "$current" ]; then
    echo "miss: changed $(diff <(echo "$recorded") <(echo "$current") | sed -nE 's/^[<>] ([^=]*)=.*/\1/p' | sort -u | tr '\n' ' ')"
    return 1
  fi
  [ -d "$NM" ] || { echo "miss: node_modules missing"; return 1; }
  local want have
  want=$(sed -n 's/^installed_lock=//p' "$MARKER")
  have=$(installed_lock_sha)
  [ "$have" != missing ] && [ "$have" = "$want" ] || { echo "miss: installed lock missing or changed"; return 1; }
  echo "hit: dependency fingerprint and installed lock match"
  return 0
}

case "$ACTION" in
  fingerprint)
    fingerprint; echo "installed_lock=$(installed_lock_sha)" ;;
  check)
    refuse_symlink
    is_hit ;;
  install)
    refuse_symlink
    if is_hit; then exit 0; fi
    rm -f "$MARKER"  # an interrupted or failed install must not leave an old marker that looks valid
    before=$(lock_sha)
    echo "npm ci in $SRC"
    (cd "$SRC" && npm ci --prefer-offline)
    rc=$?
    [ "$rc" -eq 0 ] || { echo "npm ci exit $rc; no deps.ok written" >&2; exit 4; }
    [ ! -L "$NM" ] && [ -d "$NM" ] || { echo "node_modules is not a real directory after npm ci" >&2; exit 4; }
    [ -f "$NM/.package-lock.json" ] || { echo "npm ci left no node_modules/.package-lock.json" >&2; exit 4; }
    [ "$(lock_sha)" = "$before" ] || { echo "npm ci changed package-lock.json; not a reproducible install" >&2; exit 4; }
    { fingerprint; echo "installed_lock=$(installed_lock_sha)"; echo "source=$SRC"; echo "written_by=install"; } \
      | write_marker || { echo "could not write $MARKER" >&2; exit 4; }
    echo "deps.ok written" ;;
  adopt)
    refuse_symlink
    [ -n "$RECEIPT" ] || usage
    [ -f "$RECEIPT" ] || { echo "receipt $RECEIPT not found" >&2; exit 3; }
    # The receipt is always checked, also when deps.ok already matches; a mismatch never touches the marker.
    [ -d "$NM" ] || { echo "node_modules missing; nothing to adopt" >&2; exit 4; }
    fp=$(mktemp "${TMPDIR:-/tmp}/paseo-deps-fp.XXXXXX") || exit 3
    fingerprint > "$fp"
    python3 -I - "$RECEIPT" "$SRC" "$fp" "$(installed_lock_sha)" <<'PY'
import json, os, sys
receipt_path, src, fp_path, installed = sys.argv[1:5]
cur = dict(line.split("=", 1) for line in open(fp_path).read().splitlines() if "=" in line)
os.unlink(fp_path)
try:
    r = json.load(open(receipt_path))
except (OSError, ValueError) as e:
    sys.exit(f"receipt unreadable: {e}")
post = r.get("postconditions") or {}
checks = {
    "status": r.get("status") == "success",
    "exit": r.get("exit") == 0,
    "command": str(r.get("command", "")).startswith("npm ci"),
    "worktree": os.path.realpath(str(r.get("worktree", ""))) == os.path.realpath(src),
    "lockBefore": r.get("lockSha256Before") == cur["lock"],
    "lockAfter": r.get("lockSha256After") == cur["lock"],
    "installedLock": installed != "missing" and r.get("installedLockSha256") == installed,
    "workspacePackageJsons": r.get("workspacePackageJsonsSha256") == cur["workspace_package_jsons"],
    "patches": r.get("patchesDirSha256") == cur["patches"],
    "postinstall": r.get("postinstallScriptSha256") == cur["postinstall"],
    "node": r.get("node") == cur["node"],
    "npm": r.get("npm") == cur["npm"],
    "platform": r.get("platform") == cur["platform"],
    "arch": r.get("arch") == cur["arch"],
    "nodeModulesRealDir": post.get("nodeModulesRealDir") is True,
    "hiddenLockPresent": post.get("hiddenLockPresent") is True,
    "lockUnchanged": post.get("lockUnchanged") is True,
}
bad = [k for k, ok in checks.items() if not ok]
if bad:
    print("receipt does not match this checkout: " + ", ".join(bad), file=sys.stderr)
    sys.exit(4)
PY
    rc=$?
    rm -f "$fp"
    [ "$rc" -eq 0 ] || exit 4
    if is_hit > /dev/null; then echo "receipt matches; deps.ok already valid and left unchanged"; exit 0; fi
    rm -f "$MARKER"
    { fingerprint; echo "installed_lock=$(installed_lock_sha)"; echo "source=$SRC"
      echo "written_by=adopt receipt $(sha "$RECEIPT")"; } | write_marker || exit 4
    echo "deps.ok adopted from receipt $(sha "$RECEIPT")" ;;
  *) usage ;;
esac
