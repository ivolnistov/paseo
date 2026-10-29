#!/bin/bash
# Local signed Android release build of a reviewed Paseo commit. See .agents/skills/paseo-mobile-build/SKILL.md.
# Derived from the audited 0.11.2 helper; scripts/mobile-build/PROVENANCE.md lists every change.
# Never prints the keystore password or Firebase values. Never cleans the generated project or Gradle state.
set -uo pipefail

TOOLS_DIR=$(cd "$(dirname "$0")" && pwd -P)
SCRIPT_PATH="$TOOLS_DIR/$(basename "$0")"
HELPERS="$TOOLS_DIR/mobile-build"
[ -z "${PASEO_BUILD_BIN_DIR:-}" ] || PATH="$PASEO_BUILD_BIN_DIR:$PATH"

# Environment that can change what prebuild or Metro produce. Hashed into the stamps, never printed.
# PASEO_SERVE_SIM_* only add dev-server middleware and do not change release output.
CALLER_ENV_HASH=$( { env | LC_ALL=C grep -E '^(EXPO_|EAS_|NODE_ENV=|PASEO_WEB_PLATFORM=|PASEO_FDROID_BUILD=|PASEO_PROFILE_BUILD=|PASEO_WATCHDOG_BUILD=|PASEO_DIAGNOSTIC_REVISION=|PASEO_DIAGNOSTIC_BUILD_TIME=)' || true; } \
  | LC_ALL=C sort | shasum -a 256 | cut -c1-64)

SRC=""; EXPECTED_SHA=""; VERSION=""; VARIANT=""; ABI=""; GOOGLE_SERVICES=""; KEYSTORE_DIR=""; EXPECTED_CERT=""
OUT=""; STATE=""; JAVA_HOME_IN=""; ANDROID_HOME_IN=""; PACKAGE=""; ALIAS=paseo-local-release
DEPS_RECEIPT=""; REFERENCE_APK=""; PROVENANCE=""
WITH_JVM_TESTS=0; WITH_APP_DEPS=0; FORCE_PREPARE=0; PREPARE_ONLY=0; DRY_RUN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") --src DIR --expected-sha SHA --version V --variant NAME --abi ABI
         --google-services FILE --keystore DIR --expected-cert SHA256 --out DIR
         --java-home DIR --android-home DIR [options]

Builds one signed release APK from a clean, reviewed commit, verifies it and delivers it to --out. Incremental:
dependencies, Expo prebuild and the JS bundle rerun only when their recorded fingerprint changes; Gradle runs
once with its build cache. The generated android/ project and its native intermediates are kept.

Required:
  --src DIR               Paseo checkout or worktree at the reviewed commit (must be clean)
  --expected-sha SHA      Reviewed commit; HEAD must equal it
  --version V             Release version; must equal <src>/packages/app/package.json "version" exactly,
                          prerelease suffix included. versionName/versionCode come from
                          <src>/packages/app/native-release-version.js
  --variant NAME          APP_VARIANT for prebuild and Gradle: production (sh.paseo, google-services passed as
                          GOOGLE_SERVICES_FILE_PROD) or development (sh.paseo.debug, GOOGLE_SERVICES_FILE_DEBUG).
                          Other names are refused; the source's app.config variants must declare it.
  --abi ABI               Single ABI to build, e.g. arm64-v8a
  --google-services FILE  google-services.json for the variant, mode 0600/0400
  --keystore DIR          Directory with release.p12 and storepass (mode 0600/0400)
  --expected-cert SHA256  Signer certificate digest from the owner's release record (not read from the keystore
                          under test); the keystore and the signed APK must both match it
  --out DIR               Delivery directory; an existing different file there is never overwritten
  --java-home DIR         JDK 17
  --android-home DIR      Android SDK with the build-tools, NDK and platform React Native pins

Options:
  --state-dir DIR         Fingerprints and the state lock  [<src>/.dev/paseo-mobile-build/state]. --out and
                          --state-dir may be inside --src only where git ignores them.
  --package ID            Expected application id; must match the variant  [from --variant]
  --alias NAME            Key alias  [$ALIAS]
  --deps-receipt FILE     Adopt a recorded successful npm ci of this checkout instead of reinstalling.
                          Every binding must match or the build stops (exit 4).
  --reference-apk FILE    Also require Firebase resources equal this APK's
  --provenance NAME=SHA256
                          Record a named input digest in the build manifest's "provenance" object, e.g.
                          --provenance client.patch=<sha256>. Repeatable; NAME is [A-Za-z0-9._+-] (max 128),
                          SHA256 is 64 hex. Repeating a name with a different digest is a usage error.
  --with-jvm-tests        Add :paseo-hardware-keyboard:testReleaseUnitTest to the Gradle invocation; the source must
                          have packages/app/modules/paseo-hardware-keyboard/android
  --with-app-deps         Run 'npm run build:app-deps' after deps (needed on a fresh checkout: the build stops with 3
                          when an @getpaseo workspace output the app imports is missing); forces the bundle rerun
  --force-prepare         Rerun Expo prebuild even if its stamp matches
  --prepare-only          Stop after preparation
  --dry-run               Preflight, then print the plan and cache decisions; write nothing
  -h, --help              Show this help

PASEO_FDROID_BUILD=1 is refused (exit 3): F-Droid source builds are out of scope.

Exit codes: 0 ok, 2 usage, 3 precondition, 4 build or verification failed, 5 refused overwrite.
EOF
}

die() { local code=$1; shift; echo "FAIL: $*" >&2; exit "$code"; }

# Named input digests for the manifest, one "name=sha256" per line, sorted by name.
add_provenance() {
  [[ "$1" == *=* ]] || die 2 "--provenance needs NAME=SHA256: $1"
  local name=${1%%=*} digest line
  digest=$(echo "${1#*=}" | tr '[:upper:]' '[:lower:]')
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]] || die 2 "--provenance name must be [A-Za-z0-9._+-], max 128: $name"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "--provenance $name needs a full 64-hex SHA-256"
  while IFS= read -r line; do
    [ "${line%%=*}" = "$name" ] || continue
    [ "${line#*=}" = "$digest" ] || die 2 "--provenance $name given twice with different digests"
    return 0
  done <<< "$PROVENANCE"
  PROVENANCE=$(printf '%s\n%s=%s\n' "$PROVENANCE" "$name" "$digest" | sed '/^$/d' | LC_ALL=C sort)
}

need_arg() { [ $# -ge 2 ] && [ -n "$2" ] || { usage >&2; die 2 "$1 needs a value"; }; }

while [ $# -gt 0 ]; do
  case "$1" in
    --src) need_arg "$@"; SRC=$2; shift 2 ;;
    --expected-sha) need_arg "$@"; EXPECTED_SHA=$2; shift 2 ;;
    --version) need_arg "$@"; VERSION=$2; shift 2 ;;
    --variant) need_arg "$@"; VARIANT=$2; shift 2 ;;
    --abi) need_arg "$@"; ABI=$2; shift 2 ;;
    --google-services) need_arg "$@"; GOOGLE_SERVICES=$2; shift 2 ;;
    --keystore) need_arg "$@"; KEYSTORE_DIR=$2; shift 2 ;;
    --expected-cert) need_arg "$@"; EXPECTED_CERT=$2; shift 2 ;;
    --out) need_arg "$@"; OUT=$2; shift 2 ;;
    --state-dir) need_arg "$@"; STATE=$2; shift 2 ;;
    --java-home) need_arg "$@"; JAVA_HOME_IN=$2; shift 2 ;;
    --android-home) need_arg "$@"; ANDROID_HOME_IN=$2; shift 2 ;;
    --package) need_arg "$@"; PACKAGE=$2; shift 2 ;;
    --alias) need_arg "$@"; ALIAS=$2; shift 2 ;;
    --deps-receipt) need_arg "$@"; DEPS_RECEIPT=$2; shift 2 ;;
    --reference-apk) need_arg "$@"; REFERENCE_APK=$2; shift 2 ;;
    --provenance) need_arg "$@"; add_provenance "$2"; shift 2 ;;
    --with-jvm-tests) WITH_JVM_TESTS=1; shift ;;
    --with-app-deps) WITH_APP_DEPS=1; shift ;;
    --force-prepare) FORCE_PREPARE=1; shift ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done

missing=""
for pair in "src:$SRC" "expected-sha:$EXPECTED_SHA" "version:$VERSION" "variant:$VARIANT" "abi:$ABI" \
            "google-services:$GOOGLE_SERVICES" "keystore:$KEYSTORE_DIR" "expected-cert:$EXPECTED_CERT" \
            "out:$OUT" "java-home:$JAVA_HOME_IN" "android-home:$ANDROID_HOME_IN"; do
  [ -n "${pair#*:}" ] || missing="$missing --${pair%%:*}"
done
[ -z "$missing" ] || { usage >&2; die 2 "missing required:$missing"; }
# Variants app.config.js supports, with the application id and the google-services env key it reads.
case "$VARIANT" in
  production) VARIANT_PACKAGE=sh.paseo; GS_ENV_KEY=GOOGLE_SERVICES_FILE_PROD ;;
  development) VARIANT_PACKAGE=sh.paseo.debug; GS_ENV_KEY=GOOGLE_SERVICES_FILE_DEBUG ;;
  *) die 2 "unsupported --variant $VARIANT (supported: production, development)" ;;
esac
PACKAGE=${PACKAGE:-$VARIANT_PACKAGE}
[ "$PACKAGE" = "$VARIANT_PACKAGE" ] || die 2 "--package $PACKAGE does not match variant $VARIANT ($VARIANT_PACKAGE)"
# F-Droid source builds use another config and versionCode scheme; this tool builds signed Firebase releases only.
[ "${PASEO_FDROID_BUILD:-}" != 1 ] || die 3 "PASEO_FDROID_BUILD=1 is not supported: this tool builds locally signed release APKs"
EXPECTED_CERT=$(echo "$EXPECTED_CERT" | tr '[:upper:]' '[:lower:]' | tr -d ':')

# Later steps cd into the source tree, so resolve every path against the invocation directory first.
abs() { case "$1" in /*) echo "$1" ;; *) echo "$PWD/$1" ;; esac; }
SRC=$(abs "$SRC"); GOOGLE_SERVICES=$(abs "$GOOGLE_SERVICES"); KEYSTORE_DIR=$(abs "$KEYSTORE_DIR")
OUT=$(abs "$OUT"); JAVA_HOME=$(abs "$JAVA_HOME_IN"); ANDROID_HOME=$(abs "$ANDROID_HOME_IN")
[ -z "$STATE" ] || STATE=$(abs "$STATE")
[ -z "$DEPS_RECEIPT" ] || DEPS_RECEIPT=$(abs "$DEPS_RECEIPT")
[ -z "$REFERENCE_APK" ] || REFERENCE_APK=$(abs "$REFERENCE_APK")

SRC=$(cd "$SRC" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || die 3 "--src is not a git checkout"
STATE=${STATE:-$SRC/.dev/paseo-mobile-build/state}
KEYSTORE="$KEYSTORE_DIR/release.p12"
PASSFILE="$KEYSTORE_DIR/storepass"
LOCK="$STATE/build.lock"
# One build per physical checkout, whatever --state-dir is: the lock lives in the worktree's own git dir.
SRC_LOCK="$(git -C "$SRC" rev-parse --absolute-git-dir)/paseo-mobile-build.lock"
PREP_STAMP="$STATE/prepare.stamp"
BUNDLE_STAMP="$STATE/bundle.stamp"
APP="$SRC/packages/app"
ANDROID="$APP/android"
RN="$SRC/node_modules/react-native"
BUNDLE_TASK=":app:createBundleReleaseJsAndAssets"
PKG_PATH=$(echo "$PACKAGE" | tr . /)
NAME="paseo-v$VERSION-$PACKAGE-$ABI.apk"
if [ -n "${PASEO_BUILD_NOW:-}" ]; then STAMP=$(echo "$PASEO_BUILD_NOW" | tr -d ':-'); else STAMP=$(date -u +%Y%m%dT%H%M%SZ); fi
WORK="$OUT/work-$STAMP-$$"
LOGS="$WORK/logs"
SIGNED="$WORK/$NAME"
export ANDROID_HOME JAVA_HOME
export PATH="${PASEO_BUILD_BIN_DIR:+$PASEO_BUILD_BIN_DIR:}$JAVA_HOME/bin:$PATH"

pass() { echo "PASS: $*"; }
sha() { if [ -f "$1" ]; then shasum -a 256 "$1" | cut -d' ' -f1; else echo missing; fi; }

# SDK pins come from the installed React Native version catalog. Before dependencies are installed, preflight uses
# tentative pins (the catalog of an earlier install, or the defaults below). After the deps step the installed
# catalog is authoritative and required: missing or incomplete exits 3. SDK_PINS_ORIGIN records which was used.
CATALOG="$RN/gradle/libs.versions.toml"
toml_value() { sed -nE "s/^$1 *= *\"([^\"]+)\".*/\1/p" "$CATALOG" | head -1; }
derive_sdk_pins() {  # $1: tentative|installed. Sets BUILD_TOOLS NDK_VERSION COMPILE_SDK BT SDK_PINS_ORIGIN.
  if [ -f "$CATALOG" ]; then
    BUILD_TOOLS=$(toml_value buildTools); NDK_VERSION=$(toml_value ndkVersion); COMPILE_SDK=$(toml_value compileSdk)
    if [ -z "$BUILD_TOOLS" ] || [ -z "$NDK_VERSION" ] || [ -z "$COMPILE_SDK" ]; then
      echo "FAIL: $CATALOG lacks buildTools, ndkVersion or compileSdk" >&2
      return 3
    fi
    SDK_PINS_ORIGIN="catalog ($1)"
  elif [ "$1" = installed ]; then
    echo "FAIL: the installed react-native has no $CATALOG; cannot take SDK pins from it" >&2
    return 3
  else
    # Cold checkout, nothing installed yet: tentative defaults for preflight only; the deps step decides.
    BUILD_TOOLS=36.0.0; NDK_VERSION=27.1.12297006; COMPILE_SDK=36
    SDK_PINS_ORIGIN="tentative defaults, no catalog installed yet"
  fi
  BT="$ANDROID_HOME/build-tools/$BUILD_TOOLS"
}
check_sdk_paths() {
  local t
  for t in zipalign apksigner aapt2 dexdump; do [ -x "$BT/$t" ] || { echo "FAIL: missing $BT/$t" >&2; return 3; }; done
  [ -f "$ANDROID_HOME/ndk/$NDK_VERSION/source.properties" ] || { echo "FAIL: missing NDK $NDK_VERSION in $ANDROID_HOME" >&2; return 3; }
  [ -f "$ANDROID_HOME/platforms/android-$COMPILE_SDK/android.jar" ] || { echo "FAIL: missing platform android-$COMPILE_SDK" >&2; return 3; }
}
derive_sdk_pins tentative || exit 3

check_source() {
  [ "$(git -C "$SRC" rev-parse HEAD)" = "$EXPECTED_SHA" ] || die 3 "HEAD $(git -C "$SRC" rev-parse HEAD) is not reviewed $EXPECTED_SHA"
  [ -z "$(git -C "$SRC" status --porcelain)" ] || die 3 "source has uncommitted or untracked changes"
  pass "source $EXPECTED_SHA clean ($1)"
}

check_secret_file() {
  [ -f "$1" ] || die 3 "$2 not found"
  local mode
  # BSD stat first; GNU stat treats -f as filesystem mode, prints a block and fails, so keep only a success.
  if ! mode=$(stat -f %Lp "$1" 2>/dev/null); then mode=$(stat -c %a "$1" 2>/dev/null) || die 3 "cannot stat $2"; fi
  [ "$mode" = "600" ] || [ "$mode" = "400" ] || die 3 "$2 must be mode 0600 or 0400 (is $mode)"
}

# When app.config.js has a variants table, the variant must be in it with the expected package and env key.
source_declares_variant() {
  python3 -I - "$APP/app.config.js" "$VARIANT" "$PACKAGE" "$GS_ENV_KEY" <<'PY'
import re, sys
path, variant, package, key = sys.argv[1:5]
text = open(path).read()
table = re.search(r"const variants = \{(.*?)\n\};", text, re.S)
if not table:
    sys.exit(0)  # no variants table: the built-in mapping applies
block = re.search(r"\n  " + re.escape(variant) + r": \{(.*?)\n  \},", table.group(1) + "\n", re.S)
ok = bool(block) and f'packageId: "{package}"' in block.group(1) and f'envKey: "{key}"' in block.group(1)
sys.exit(0 if ok else 1)
PY
}

# versionName and versionCode exactly as the app config derives them.
native_version() {
  node -e 'const m = require(process.argv[1]); const r = m.getNativeReleaseVersion(process.argv[2]);
    if (!r || !r.appVersion || !Number.isSafeInteger(r.androidVersionCode)) process.exit(1);
    console.log(`${r.androidVersionName ?? r.appVersion} ${r.androidVersionCode}`);' "$APP/native-release-version.js" "$VERSION"
}

# Lowercase hex SHA-256 of the keystore's certificate; the password goes to keytool only as a file.
keystore_cert() {
  keytool -list -v -keystore "$KEYSTORE" -storetype PKCS12 -alias "$ALIAS" -storepass:file "$PASSFILE" 2>/dev/null \
    | sed -nE 's/^[[:space:]]*SHA256:[[:space:]]*([0-9A-Fa-f:]+).*/\1/p' | head -1 | tr -d ':' | tr '[:upper:]' '[:lower:]'
}

# --out and --state-dir may lie inside --src only where git ignores them; otherwise the run would dirty the source.
check_output_locations() {
  local label path rel
  for label in out state-dir; do
    if [ "$label" = out ]; then path=$OUT; else path=$STATE; fi
    rel=$(python3 -I -c 'import os,sys
src, p = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])
print(os.path.relpath(p, src) if os.path.commonpath([src, p]) == src else "")' "$SRC" "$path") || die 3 "cannot resolve --$label"
    [ -n "$rel" ] || continue
    [ "$rel" != . ] && git -C "$SRC" check-ignore -q -- "$rel" \
      || die 3 "--$label $path is inside the source and not ignored by git; use an ignored or outside directory"
  done
}

# Describes a lock that is present: its owner alive, dead (stale) or unreadable. Prints nothing and fails when absent.
# Never takes over, deletes or signals the holder.
held_lock() {
  [ -e "$1" ] || return 1
  local pid
  pid=$( { tr -d ' \n' < "$1/pid"; } 2>/dev/null)
  case "$pid" in
    ""|*[!0-9]*) echo "$1 has no readable owner pid; refusing. Remove it by hand after checking that no build runs." ;;
    *) if kill -0 "$pid" 2>/dev/null; then echo "$1 is held by running pid $pid"
       else echo "stale lock $1: owner pid $pid is dead; refusing. Remove it by hand after checking that no build runs."; fi ;;
  esac
}

preflight() {
  check_source preflight
  local held
  held=$(held_lock "$SRC_LOCK") && die 3 "source lock: $held"
  held=$(held_lock "$LOCK") && die 3 "state lock: $held"
  check_output_locations
  [ -x "$JAVA_HOME/bin/java" ] || die 3 "no java in $JAVA_HOME"
  "$JAVA_HOME/bin/java" -version 2>&1 | head -1 | grep -q '"17\.' || die 3 "$JAVA_HOME is not JDK 17"
  check_sdk_paths || exit 3
  if [ "$WITH_JVM_TESTS" = 1 ] && [ ! -d "$APP/modules/paseo-hardware-keyboard/android" ]; then
    die 3 "--with-jvm-tests needs packages/app/modules/paseo-hardware-keyboard/android in the source"
  fi
  command -v node >/dev/null && command -v npm >/dev/null && command -v python3 >/dev/null || die 3 "node, npm and python3 are required"
  [ ! -L "$SRC/node_modules" ] || die 3 "node_modules is a symlink; use a real install in $SRC"
  [ -f "$APP/native-release-version.js" ] || die 3 "missing packages/app/native-release-version.js"
  local src_version
  src_version=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$APP/package.json" 2>/dev/null) \
    || die 3 "cannot read the version in packages/app/package.json"
  [ "$src_version" = "$VERSION" ] || die 3 "--version $VERSION is not the source version $src_version (packages/app/package.json)"
  source_declares_variant || die 3 "the source app.config.js does not declare variant $VARIANT with $PACKAGE and $GS_ENV_KEY"
  native_version > /dev/null || die 3 "cannot derive native version for $VERSION"
  check_secret_file "$GOOGLE_SERVICES" "google-services file"
  python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if [c for c in d.get("client", []) if c["client_info"]["android_client_info"]["package_name"]==sys.argv[2]] else 1)' \
    "$GOOGLE_SERVICES" "$PACKAGE" 2>/dev/null || die 3 "google-services has no client for $PACKAGE"
  check_secret_file "$KEYSTORE" keystore
  check_secret_file "$PASSFILE" "password file"
  [ "$(keystore_cert)" = "$EXPECTED_CERT" ] || die 3 "keystore certificate is not $EXPECTED_CERT"
  [ -z "$DEPS_RECEIPT" ] || [ -f "$DEPS_RECEIPT" ] || die 3 "deps receipt not found"
  [ -z "$REFERENCE_APK" ] || [ -f "$REFERENCE_APK" ] || die 3 "reference APK not found"
  pass "preflight (JDK 17, build-tools $BUILD_TOOLS, NDK $NDK_VERSION, android-$COMPILE_SDK [$SDK_PINS_ORIGIN], signer, no concurrent build)"
}

# ---- Preparation stamp -------------------------------------------------------------------------------
prep_fingerprint() {
  echo "schema=paseo-mobile-build-prepare-v1"
  echo "head=$(git -C "$SRC" rev-parse HEAD)"
  echo "tree=$(git -C "$SRC" rev-parse 'HEAD^{tree}')"
  echo "script=$(sha "$SCRIPT_PATH")"
  echo "package_lock=$(sha "$SRC/package-lock.json")"
  echo "installed_lock=$(sha "$SRC/node_modules/.package-lock.json")"
  echo "react_native=$(sha "$RN/package.json")"
  echo "expo_package=$(sha "$SRC/node_modules/expo/package.json")"
  echo "node=$(node -v 2>/dev/null)"
  echo "java=$("$JAVA_HOME/bin/java" -version 2>&1 | head -1)"
  echo "java_home=$JAVA_HOME"
  echo "android_home=$ANDROID_HOME"
  echo "sdk=build-tools $BUILD_TOOLS, ndk $NDK_VERSION, android-$COMPILE_SDK"  # the pins; their origin is reported, not hashed
  echo "platform=$(uname -sm)"
  echo "firebase=$(sha "$GOOGLE_SERVICES")"
  echo "identity=$PACKAGE/$VERSION/$VERSION_NAME/$VERSION_CODE/$VARIANT/$ABI"
  echo "caller_env=$CALLER_ENV_HASH"
}

NATIVE_FILES="app/build.gradle build.gradle settings.gradle gradle.properties gradle/wrapper/gradle-wrapper.properties
app/src/main/AndroidManifest.xml app/src/main/java/$PKG_PATH/MainActivity.kt app/src/main/java/$PKG_PATH/MainApplication.kt
app/google-services.json gradlew"

native_hashes() { local f; for f in $NATIVE_FILES; do echo "native:$f=$(sha "$ANDROID/$f")"; done; }
prep_state() { prep_fingerprint; native_hashes; }

main_activity_manifest_line() { grep 'android:name=".MainActivity"' "$ANDROID/app/src/main/AndroidManifest.xml"; }

validate_native() {
  local f
  for f in $NATIVE_FILES; do [ -f "$ANDROID/$f" ] || die 4 "generated file missing: android/$f"; done
  [ -x "$ANDROID/gradlew" ] || die 4 "android/gradlew not executable"
  cmp -s "$ANDROID/app/google-services.json" "$GOOGLE_SERVICES" || die 4 "android/app/google-services.json differs from input"
  grep -q "apply plugin: 'com.google.gms.google-services'" "$ANDROID/app/build.gradle" || die 4 "google-services plugin not applied"
  grep -q "com.google.gms:google-services" "$ANDROID/build.gradle" || die 4 "google-services classpath missing"
  grep -q "applicationId '$PACKAGE'" "$ANDROID/app/build.gradle" || die 4 "applicationId is not $PACKAGE"
  grep -q "versionCode $VERSION_CODE\$" "$ANDROID/app/build.gradle" || die 4 "versionCode is not $VERSION_CODE"
  grep -q "versionName \"$VERSION_NAME\"" "$ANDROID/app/build.gradle" || die 4 "versionName is not $VERSION_NAME"
  # Gates for config plugins the source declares: their output must be in the generated project.
  if [ -f "$APP/modules/paseo-hardware-keyboard/app.plugin.js" ]; then
    local activity="$ANDROID/app/src/main/java/$PKG_PATH/MainActivity.kt"
    grep -q "override fun dispatchKeyEvent(event: android.view.KeyEvent): Boolean" "$activity" \
      && grep -q "sh.paseo.hardwarekeyboard.PaseoHardwareKeyboardDispatcher.dispatchKeyEvent(this, event)" "$activity" \
      || die 4 "MainActivity lacks the hardware keyboard dispatch"
  fi
  if [ -f "$APP/plugins/with-android-fold-configuration.js" ]; then
    main_activity_manifest_line | grep -q 'android:configChanges="[^"]*smallestScreenSize' \
      || die 4 "MainActivity configChanges lack smallestScreenSize (fold configuration)"
  fi
  if grep -q 'orientation: "default"' "$APP/app.config.js"; then
    ! main_activity_manifest_line | grep -q 'android:screenOrientation="portrait"' \
      || die 4 "MainActivity is locked to portrait although app.config uses orientation default"
  fi
}

diff_keys() { diff <(printf '%s\n' "$1") <(printf '%s\n' "$2") | sed -nE 's/^[<>] ([^=]*)=.*/\1/p' | sort -u | tr '\n' ' '; }

prep_decision() {
  [ "$FORCE_PREPARE" = 1 ] && { echo "run --force-prepare"; return; }
  [ -f "$PREP_STAMP" ] || { echo "run no valid stamp"; return; }
  local current recorded keys
  current=$(prep_state); recorded=$(cat "$PREP_STAMP")
  if [ "$current" != "$recorded" ]; then
    keys=$(diff_keys "$recorded" "$current"); echo "run changed: ${keys:-stamp format}"; return
  fi
  ( validate_native ) >/dev/null 2>&1 || { echo "run generated project fails validation"; return; }
  echo "skip stamp matches and generated project validates"
}

# ---- JS bundle stamp ---------------------------------------------------------------------------------
# The bundle task tracks only packages/app; workspace packages and node_modules are outside it.
bundle_fingerprint() {
  echo "schema=paseo-mobile-build-bundle-v1"
  echo "tree=$(git -C "$SRC" rev-parse 'HEAD^{tree}')"
  echo "package_lock=$(sha "$SRC/package-lock.json")"
  echo "installed_lock=$(sha "$SRC/node_modules/.package-lock.json")"
  echo "node=$(node -v 2>/dev/null)"
  echo "identity=$VARIANT/$ABI"
  echo "caller_env=$CALLER_ENV_HASH"
  # Workspace packages the bundle imports are built outside packages/app; their entry outputs feed the bundle.
  echo "app_deps_outputs=$(python3 -I "$HELPERS/app_deps_outputs.py" --src "$SRC" 2>/dev/null | while read -r f; do sha "$f"; done | shasum -a 256 | cut -c1-64)"
}

# Fails (3) when an @getpaseo workspace output the app imports is missing; --with-app-deps builds them.
check_app_deps_outputs() {
  python3 -I "$HELPERS/app_deps_outputs.py" --src "$SRC" > /dev/null \
    || { echo "FAIL: workspace package outputs are missing; rerun with --with-app-deps" >&2; return 3; }
}

bundle_decision() {
  [ "$WITH_APP_DEPS" = 1 ] && { echo "rerun --with-app-deps rebuilt workspace packages"; return; }
  [ -f "$BUNDLE_STAMP" ] || { echo "rerun no stamp from a successful build"; return; }
  local current recorded
  current=$(bundle_fingerprint); recorded=$(cat "$BUNDLE_STAMP")
  [ "$current" = "$recorded" ] || { echo "rerun changed: $(diff_keys "$recorded" "$current")"; return; }
  echo "gradle unchanged since last successful build; Gradle decides"
}

deps_decision() {
  local out rc
  out=$(bash "$HELPERS/deps-state.sh" check --src "$SRC" --state-dir "$STATE" 2>&1); rc=$?
  case "$rc" in
    0) echo "reuse ($out)" ;;
    1) if [ -n "$DEPS_RECEIPT" ]; then echo "adopt receipt $DEPS_RECEIPT ($out)"; else echo "npm ci ($out)"; fi ;;
    *) echo "refused ($out)" ;;
  esac
}

write_stamp() { local tmp="$1.tmp.$$"; cat > "$tmp" && mv -f "$tmp" "$1"; }

gradle_tasks() {
  local tasks=":app:assembleRelease"
  [ "$WITH_JVM_TESTS" = 1 ] && tasks=":paseo-hardware-keyboard:testReleaseUnitTest $tasks"
  [ "${BUNDLE_RERUN:-0}" = 1 ] && tasks="$BUNDLE_TASK --rerun $tasks"
  echo "$tasks"
}
# One worker, no daemon, no parallel projects. --build-cache reuses cacheable outputs; --profile and --info
# record why each task ran (local only, no scan upload).
GRADLE_FLAGS="-PreactNativeArchitectures=$ABI --no-daemon --max-workers=1 --no-parallel -Dorg.gradle.parallel=false --build-cache --profile --info --console=plain"

plan() {
  cat <<EOF
Plan:
  source     $SRC @ $EXPECTED_SHA
  identity   $PACKAGE $VERSION -> versionName $VERSION_NAME, versionCode $VERSION_CODE; variant $VARIANT; $ABI only
  signer     $EXPECTED_CERT
  sdk        build-tools $BUILD_TOOLS, NDK $NDK_VERSION, android-$COMPILE_SDK ($SDK_PINS_ORIGIN; rechecked after deps); JDK $JAVA_HOME
  deliver    $OUT/$NAME (+ .sha256, .build-manifest.json); logs in $OUT/work-<stamp>/
  state      $STATE
  provenance $([ -n "$PROVENANCE" ] && echo "$PROVENANCE" | tr '\n' ' ' || echo none)
  deps       $DEPS_DECISION
  app deps   $([ "$WITH_APP_DEPS" = 1 ] && echo "npm run build:app-deps" || echo "not requested")
  prepare    $PREP_DECISION
             (APP_VARIANT=$VARIANT $GS_ENV_KEY=<file> npx expo prebuild --platform android --non-interactive --no-install; existing android/ kept)
  bundle     $BUNDLE_DECISION
  gradle     $([ "$PREPARE_ONLY" = 1 ] && echo "skipped (--prepare-only)" || echo "./gradlew $(gradle_tasks) $GRADLE_FLAGS")
  sign       $([ "$PREPARE_ONLY" = 1 ] && echo "skipped (--prepare-only)" || echo "zipalign -P 16 -f 4; apksigner sign v2+v3 --ks-pass file:<passfile>; verify; deliver")
EOF
}

deps() {
  set -uo pipefail
  if [ -n "$DEPS_RECEIPT" ]; then
    bash "$HELPERS/deps-state.sh" adopt --src "$SRC" --state-dir "$STATE" --receipt "$DEPS_RECEIPT" \
      || die 4 "deps receipt not adoptable for this checkout; not reinstalling silently"
  fi
  bash "$HELPERS/deps-state.sh" install --src "$SRC" --state-dir "$STATE" > "$LOGS/deps.log" 2>&1 \
    || { tail -20 "$LOGS/deps.log"; die 4 "dependency install failed; see $LOGS/deps.log"; }
  cat "$LOGS/deps.log" | grep -E '^(hit|miss|deps.ok|npm ci in)' || true
  check_source "after deps"
  pass "dependencies"
}

app_deps() {
  set -euo pipefail
  check_source "before app deps"
  npm --prefix "$SRC" run build:app-deps > "$LOGS/app-deps.log" 2>&1 || die 4 "build:app-deps failed; see $LOGS/app-deps.log"
  check_source "after app deps"
  pass "build:app-deps"
}

prepare() {
  set -uo pipefail
  check_source start
  cd "$APP" || die 3 "no $APP"
  # Prebuild regenerates this file from the variant's env key. Removing only it, so a copy left by another
  # variant cannot pass validation, keeps the rest of android/ and its native intermediates.
  rm -f "$ANDROID/app/google-services.json"
  env APP_VARIANT="$VARIANT" "$GS_ENV_KEY=$GOOGLE_SERVICES" \
    npx expo prebuild --platform android --non-interactive --no-install > "$LOGS/prebuild.log" 2>&1 \
    || die 4 "expo prebuild failed; see $LOGS/prebuild.log"
  validate_native
  check_source "after prebuild"
  mkdir -p "$STATE"
  prep_state | write_stamp "$PREP_STAMP"
  pass "$VARIANT prebuild with google-services and plugin gates; stamp written"
}

summarize_gradle() {
  local log="$LOGS/gradle.log"
  {
    grep -E "^BUILD (SUCCESSFUL|FAILED)|actionable tasks" "$log" || true
    echo "--- task outcomes (EXECUTED = ran)"
    { grep -E "^> Task :" "$log" || true; } | awk '{print (NF >= 4 ? $4 : "EXECUTED")}' | sort | uniq -c
    echo "--- tasks that ran, with Gradle's reasons"
    awk '/is not up-to-date because:/{print; n=6; next} n > 0 && /^  /{print; n--; next} {n=0}' "$log"
  } > "$LOGS/gradle-summary.txt" 2>&1
  if [ -d "$ANDROID/build/reports/profile" ]; then
    cp -R "$ANDROID/build/reports/profile" "$LOGS/gradle-profile" || echo "WARNING: could not copy Gradle profile report"
  fi
  gzip -f "$log" || echo "WARNING: could not compress $log; left uncompressed"
}

build() {
  set -uo pipefail
  cd "$ANDROID" || die 3 "no $ANDROID"
  rm -f "$BUNDLE_STAMP"  # only a successful build may record the bundle inputs
  echo "./gradlew $(gradle_tasks) $GRADLE_FLAGS"
  local grc=0
  # shellcheck disable=SC2046,SC2086
  env APP_VARIANT="$VARIANT" "$GS_ENV_KEY=$GOOGLE_SERVICES" NODE_ENV=production \
    ./gradlew $(gradle_tasks) $GRADLE_FLAGS > "$LOGS/gradle.log" 2>&1 || grc=$?
  [ "$grc" -eq 0 ] || tail -40 "$LOGS/gradle.log"
  summarize_gradle || echo "WARNING: Gradle diagnostics incomplete"
  [ "$grc" -eq 0 ] || die 4 "Gradle exit $grc; see $LOGS/gradle.log(.gz) and gradle-summary.txt"
  pass "gradle $(gradle_tasks)"
  check_source "after gradle"
  mkdir -p "$STATE"
  bundle_fingerprint | write_stamp "$BUNDLE_STAMP"
}

# DEX classes and Expo registry entries the source must ship, derived from what it declares.
expected_modules() {
  [ -d "$APP/modules/paseo-hardware-keyboard/android" ] && echo "sh/paseo/hardwarekeyboard/PaseoHardwareKeyboardModule registry"
  [ -d "$APP/modules/paseo-hardware-keyboard/android" ] && echo "sh/paseo/hardwarekeyboard/PaseoHardwareKeyboardDispatcher class"
  [ -d "$APP/modules/paseo-hardware-keyboard/android" ] && echo "sh/paseo/hardwarekeyboard/SubmitKeySequence class"
  [ -d "$APP/modules/paseo-screen-geometry/android" ] && echo "sh/paseo/screengeometry/PaseoScreenGeometryModule registry"
  grep -q '"expo-screen-orientation"' "$APP/package.json" && echo "expo/modules/screenorientation/ScreenOrientationModule registry"
  return 0
}

verify() {
  set -euo pipefail
  local src_apk="$ANDROID/app/build/outputs/apk/release/app-release.apk"
  cp "$src_apk" "$WORK/gradle-release.apk" || die 4 "Gradle produced no $src_apk"
  "$BT/zipalign" -P 16 -f 4 "$WORK/gradle-release.apk" "$WORK/aligned.apk" || die 4 "zipalign failed"
  "$BT/apksigner" sign --ks "$KEYSTORE" --ks-type PKCS12 --ks-key-alias "$ALIAS" \
    --ks-pass "file:$PASSFILE" --alignment-preserved true \
    --v1-signing-enabled false --v2-signing-enabled true --v3-signing-enabled true \
    --out "$SIGNED" "$WORK/aligned.apk" || die 4 "apksigner sign failed"
  pass "aligned and signed"

  "$BT/zipalign" -c -P 16 -v 4 "$SIGNED" > "$WORK/zipalign-check.txt" 2>&1 \
    || { grep -v "(OK" "$WORK/zipalign-check.txt" | head -20; die 4 "zipalign -c -P 16 4"; }
  pass "zipalign -c -P 16 4"

  # Default verify covers the APK's own range, where apksigner checks only v3. API 24+ forces v2 and v3.
  local vrc label file
  for label in default api24; do
    file="$WORK/apksigner-verify-$label.txt"
    vrc=0
    if [ "$label" = default ]; then
      "$BT/apksigner" verify -v --print-certs "$SIGNED" > "$file" 2>&1 || vrc=$?
    else
      "$BT/apksigner" verify -v --print-certs --min-sdk-version 24 "$SIGNED" > "$file" 2>&1 || vrc=$?
    fi
    echo "apksigner verify ($label): exit $vrc"
    grep -E "^Verified using v[1-4]|^Number of signers|certificate SHA-256|WARNING|ERROR" "$file" | sed "s/^/  [$label] /" || true
    [ "$vrc" -eq 0 ] || die 4 "apksigner verify ($label) exit $vrc"
    grep -qx "Number of signers: 1" "$file" || die 4 "signer count != 1 ($label)"
    [ "$(grep -E "^Signer #1 certificate SHA-256 digest:" "$file" | awk '{print $NF}' | sort -u)" = "$EXPECTED_CERT" ] \
      || die 4 "signer certificate mismatch ($label)"
  done
  grep -qx "Verified using v3 scheme (APK Signature Scheme v3): true" "$WORK/apksigner-verify-default.txt" || die 4 "v3 not verified (default)"
  grep -qx "Verified using v2 scheme (APK Signature Scheme v2): true" "$WORK/apksigner-verify-api24.txt" || die 4 "v2 not verified (api24)"
  grep -qx "Verified using v3 scheme (APK Signature Scheme v3): true" "$WORK/apksigner-verify-api24.txt" || die 4 "v3 not verified (api24)"
  pass "apksigner verify default and API 24+; v2+v3; single signer $EXPECTED_CERT"

  "$BT/aapt2" dump badging "$SIGNED" > "$WORK/badging.txt" || die 4 "aapt2 dump badging failed"
  grep -q "^package: name='$PACKAGE' versionCode='$VERSION_CODE' versionName='$VERSION_NAME'" "$WORK/badging.txt" || die 4 "package/version mismatch"
  grep -qx "native-code: '$ABI'" "$WORK/badging.txt" || die 4 "native-code is not $ABI only"
  unzip -Z1 "$SIGNED" > "$WORK/entries.txt" || die 4 "cannot list $SIGNED"
  [ "$(grep '^lib/' "$WORK/entries.txt" | cut -d/ -f2 | sort -u | tr '\n' ' ')" = "$ABI " ] || die 4 "unexpected lib ABI set"
  pass "$PACKAGE $VERSION_NAME ($VERSION_CODE), $ABI only"

  grep -qx "assets/index.android.bundle" "$WORK/entries.txt" || die 4 "JS bundle missing"
  unzip -p "$SIGNED" assets/index.android.bundle > "$WORK/index.android.bundle" || die 4 "cannot extract the JS bundle"
  [ "$(head -c 8 "$WORK/index.android.bundle" | xxd -p)" = "c61fbc03c103191f" ] || die 4 "bundle is not Hermes bytecode"
  python3 -I "$HELPERS/verify_apk.py" native-libs --apk "$SIGNED" --rn-root "$RN" --abi "$ABI" || die 4 "native libraries"
  pass "Hermes bundle and native libraries"

  mkdir -p "$WORK/dex"
  unzip -q -o "$SIGNED" 'classes*.dex' -d "$WORK/dex" || die 4 "cannot extract DEX files"
  : > "$WORK/dexdump.txt"
  for d in "$WORK"/dex/*.dex; do "$BT/dexdump" -d "$d" >> "$WORK/dexdump.txt" 2>/dev/null || die 4 "dexdump failed on $d"; done
  grep -q "Class descriptor *: 'Lexpo/modules/ExpoModulesPackageList;'" "$WORK/dexdump.txt" || die 4 "DEX class missing: ExpoModulesPackageList"
  awk '/Class descriptor/{f=/Lexpo\/modules\/ExpoModulesPackageList/} f' "$WORK/dexdump.txt" \
    | grep -oE 'L[A-Za-z0-9_/$]+Module;' | sed 's/^L//' | sort -u > "$WORK/registry-refs.txt" || true
  local cls kind
  while read -r cls kind; do
    [ -n "$cls" ] || continue
    grep -q "Class descriptor *: 'L$cls;'" "$WORK/dexdump.txt" || die 4 "DEX class missing: $cls"
    [ "$kind" != registry ] || grep -qx "$cls;" "$WORK/registry-refs.txt" || die 4 "Expo registry lacks $cls"
  done < <(expected_modules)
  pass "declared native modules in DEX and Expo registry"

  python3 -I "$HELPERS/payload_compare.py" "$WORK/gradle-release.apk" "$SIGNED" || die 4 "payload differs beyond signature metadata"
  python3 -I "$HELPERS/firebase_resources.py" json "$BT/aapt2" "$GOOGLE_SERVICES" "$PACKAGE" "$SIGNED" || die 4 "Firebase resources differ from google-services"
  if [ -n "$REFERENCE_APK" ]; then
    python3 -I "$HELPERS/firebase_resources.py" apk "$BT/aapt2" "$REFERENCE_APK" "$SIGNED" || die 4 "Firebase resources differ from reference APK"
  fi
  pass "payload and Firebase resources"
  check_source end
}

write_manifest() {
  python3 -I - "$@" <<'PY'
import hashlib, json, os, sys
(out, apk, src_sha, src_tree, version, vname, vcode, variant, package, abi, cert, lock, installed,
 gs, node, npm, java, sdk, rn, expo, deps, prep, bundle, timings, tools_sha, tools_head, work,
 provenance, android) = sys.argv[1:30]
data = open(apk, "rb").read()


def file_sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


# Read-only identity of the Gradle the generated project's wrapper resolves; Gradle itself is not invoked.
props_path = os.path.join(android, "gradle/wrapper/gradle-wrapper.properties")
props = {}
try:
    for line in open(props_path):
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            props[key.strip()] = value.strip().replace("\\:", ":")
except OSError:
    pass
gradle_wrapper = {
    "distributionUrl": props.get("distributionUrl"),
    "distributionSha256Sum": props.get("distributionSha256Sum"),
    "propertiesSha256": file_sha(props_path),
    "wrapperJarSha256": file_sha(os.path.join(android, "gradle/wrapper/gradle-wrapper.jar")),
}
manifest = {
    "schema": "paseo-mobile-build-manifest-v1",
    "artifact": {"file": os.path.basename(apk), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
    "source": {"sha": src_sha, "tree": src_tree},
    "version": version, "versionName": vname, "versionCode": int(vcode),
    "variant": variant, "package": package, "abi": abi, "signerCertSha256": cert,
    "inputs": {"packageLockSha256": lock, "installedLockSha256": installed, "googleServicesSha256": gs},
    "toolchain": {"node": node, "npm": npm, "java": java, "sdk": sdk, "reactNative": rn, "expo": expo,
                  "gradleWrapper": gradle_wrapper},
    "provenance": dict(line.split("=", 1) for line in provenance.splitlines() if "=" in line),
    "cache": {"dependencies": deps, "prebuild": prep, "jsBundle": bundle},
    "timings": open(timings).read().splitlines() if os.path.exists(timings) else [],
    "tool": {"buildApkSha256": tools_sha, "toolsHead": tools_head},
    "workDir": work,
}
with open(out, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY
}

# 0 when two manifests describe the same artifact and inputs. Only run metadata may differ: cache decisions,
# timings and the work directory. An unreadable manifest never matches.
same_build_identity() {
  python3 -I - "$1" "$2" <<'PY'
import json, sys
RUN_METADATA = {"cache", "timings", "workDir"}
try:
    a, b = (json.load(open(p)) for p in sys.argv[1:3])
except (OSError, ValueError):
    sys.exit(1)
if not (isinstance(a, dict) and isinstance(b, dict)):
    sys.exit(1)
strip = lambda m: {k: v for k, v in m.items() if k not in RUN_METADATA}
sys.exit(0 if strip(a) == strip(b) else 1)
PY
}

# Copy to a temp name, then hard-link into place: an existing file is never replaced.
place() {
  local from=$1 to=$2 tmp="$2.tmp.$$"
  if [ -e "$to" ]; then
    cmp -s "$from" "$to" && { echo "unchanged: $to"; return 0; }
    die 5 "$to exists with different content; not overwritten. Use a new --out."
  fi
  cp "$from" "$tmp" && ln "$tmp" "$to" 2>/dev/null; local rc=$?
  rm -f "$tmp"
  [ "$rc" -eq 0 ] && cmp -s "$from" "$to" || die 4 "could not place $to"
}

deliver() {
  set -uo pipefail
  local apk_sha
  apk_sha=$(sha "$SIGNED")
  local manifest="$WORK/$NAME.build-manifest.json" tools_head
  tools_head=$(git -C "$TOOLS_DIR" rev-parse HEAD 2>/dev/null || echo unknown)
  [ -z "$(git -C "$TOOLS_DIR" status --porcelain -- . 2>/dev/null)" ] || tools_head="$tools_head-dirty"
  write_manifest "$manifest" "$SIGNED" "$EXPECTED_SHA" "$(git -C "$SRC" rev-parse 'HEAD^{tree}')" "$VERSION" \
    "$VERSION_NAME" "$VERSION_CODE" "$VARIANT" "$PACKAGE" "$ABI" "$EXPECTED_CERT" "$(sha "$SRC/package-lock.json")" \
    "$(sha "$SRC/node_modules/.package-lock.json")" "$(sha "$GOOGLE_SERVICES")" "$(node -v)" "$(npm -v)" \
    "$("$JAVA_HOME/bin/java" -version 2>&1 | head -1)" "build-tools $BUILD_TOOLS, ndk $NDK_VERSION, android-$COMPILE_SDK; pins from $SDK_PINS_ORIGIN" \
    "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$RN/package.json" 2>/dev/null)" \
    "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$SRC/node_modules/expo/package.json" 2>/dev/null)" \
    "$DEPS_DECISION" "$PREP_DECISION" "$BUNDLE_DECISION" "$TIMINGS" "$(sha "$SCRIPT_PATH")" "$tools_head" "$WORK" \
    "$PROVENANCE" "$ANDROID" \
    || die 4 "could not write build manifest"
  echo "$apk_sha  $NAME" > "$WORK/$NAME.sha256"
  # Check the whole set first, so a refusal writes none of it. An existing manifest is kept only when it
  # records the same build identity as this run; anything else would leave a receipt that does not match.
  local f keep_manifest=0
  for f in "$NAME" "$NAME.sha256"; do
    [ ! -e "$OUT/$f" ] || cmp -s "$WORK/$f" "$OUT/$f" || die 5 "$OUT/$f exists with different content; not overwritten"
  done
  if [ -e "$OUT/$NAME.build-manifest.json" ] && ! cmp -s "$manifest" "$OUT/$NAME.build-manifest.json"; then
    same_build_identity "$manifest" "$OUT/$NAME.build-manifest.json" \
      || die 5 "$OUT/$NAME.build-manifest.json records a different build identity; nothing delivered. This run's manifest: $manifest"
    keep_manifest=1
  fi
  place "$SIGNED" "$OUT/$NAME"
  place "$WORK/$NAME.sha256" "$OUT/$NAME.sha256"
  if [ "$keep_manifest" = 1 ]; then
    echo "identical build already delivered; its manifest is kept (only run metadata differs). This run's manifest: $manifest"
  else
    place "$manifest" "$OUT/$NAME.build-manifest.json"
  fi
  (cd "$OUT" && shasum -a 256 -c "$NAME.sha256") || die 4 "delivered checksum mismatch"
  echo "delivered $OUT/$NAME"
  cat "$OUT/$NAME.sha256"
}

( preflight ) || exit $?
read -r VERSION_NAME VERSION_CODE < <(native_version)
DEPS_DECISION=$(deps_decision)
PREP_DECISION=$(prep_decision)
BUNDLE_DECISION=$(bundle_decision)
if [ "$DRY_RUN" = 1 ]; then
  BUNDLE_RERUN=0; case "$BUNDLE_DECISION" in rerun*) BUNDLE_RERUN=1 ;; esac
  plan
  echo "DRY RUN: nothing written, nothing built"
  exit 0
fi

# Source lock first, then the state lock; release only what this run owns (lock.sh checks the pid).
bash "$HELPERS/lock.sh" acquire --lock-dir "$SRC_LOCK" --pid "$$" || exit 3
trap 'bash "$HELPERS/lock.sh" release --lock-dir "$LOCK" --pid "$$" >/dev/null 2>&1
      bash "$HELPERS/lock.sh" release --lock-dir "$SRC_LOCK" --pid "$$" >/dev/null 2>&1' EXIT
bash "$HELPERS/lock.sh" acquire --lock-dir "$LOCK" --pid "$$" || exit 3
# Ctrl-C signals the foreground process group, so the running step receives it too. A TERM sent to this pid alone
# is handled after the current foreground step finishes; the locks stay held until the build exits, so no second
# run can start while that step still runs.
trap 'exit 130' INT TERM HUP

# Decide again under the lock: another run may have changed the state since the unlocked preview.
DEPS_DECISION=$(deps_decision)
PREP_DECISION=$(prep_decision)
BUNDLE_DECISION=$(bundle_decision)
BUNDLE_RERUN=0; case "$BUNDLE_DECISION" in rerun*) BUNDLE_RERUN=1 ;; esac
case "$DEPS_DECISION" in refused*) die 3 "dependencies: $DEPS_DECISION" ;; esac

mkdir -p "$LOGS" || die 3 "cannot create $LOGS"
MAIN_LOG="$LOGS/build.log"
TIMINGS="$LOGS/timings.txt"
plan | tee "$LOGS/plan.txt"
echo "logs: $LOGS"

steps="deps"
[ "$WITH_APP_DEPS" = 1 ] && steps="$steps app_deps"
steps="$steps gates prepare"
[ "$PREPARE_ONLY" = 1 ] || steps="$steps build verify deliver"

rc=0
failed=""
total_start=$(date +%s)
for step in $steps; do
  echo "== $step $(date -u +%FT%TZ)" | tee -a "$MAIN_LOG"
  start=$(date +%s)
  # After deps (and app deps): the installed catalog is authoritative and the workspace outputs must exist.
  # This runs in this shell so the re-derived SDK pins reach every later step and the manifest.
  if [ "$step" = gates ]; then
    rc=0
    { derive_sdk_pins installed && check_sdk_paths && check_app_deps_outputs; } >> "$MAIN_LOG" 2>&1 || rc=3
    [ "$rc" -eq 0 ] && echo "PASS: sdk pins from $SDK_PINS_ORIGIN: build-tools $BUILD_TOOLS, NDK $NDK_VERSION, android-$COMPILE_SDK; workspace outputs present" >> "$MAIN_LOG"
    echo "$step $(( $(date +%s) - start ))s exit $rc" | tee -a "$TIMINGS"
    [ "$rc" -eq 0 ] || { failed=$step; break; }
    continue
  fi
  # deps can change the installed lock, so decide preparation and the bundle rerun after it.
  if [ "$step" = prepare ]; then
    PREP_DECISION=$(prep_decision)
    case "$PREP_DECISION" in
      skip*) echo "prepare skipped: ${PREP_DECISION#skip }" | tee -a "$MAIN_LOG" "$TIMINGS"; continue ;;
      *) rm -f "$PREP_STAMP"; echo "prepare: $PREP_DECISION" | tee -a "$MAIN_LOG" ;;  # a failed preparation leaves no stamp
    esac
  fi
  if [ "$step" = build ]; then
    BUNDLE_DECISION=$(bundle_decision)
    BUNDLE_RERUN=0; case "$BUNDLE_DECISION" in rerun*) BUNDLE_RERUN=1 ;; esac
    echo "bundle: $BUNDLE_DECISION" | tee -a "$MAIN_LOG"
  fi
  ( "$step" ) >> "$MAIN_LOG" 2>&1
  rc=$?
  # Keep the documented codes; any other status from a tool that escaped its own check is a build failure.
  case "$rc" in 0|3|4|5|130) ;; *) echo "$step: unexpected exit $rc, reported as 4" >> "$MAIN_LOG"; rc=4 ;; esac
  echo "$step $(( $(date +%s) - start ))s exit $rc" | tee -a "$TIMINGS"
  [ "$rc" -eq 0 ] || { failed=$step; break; }
done
echo "total $(( $(date +%s) - total_start ))s" | tee -a "$TIMINGS"
if [ -n "$failed" ]; then
  echo "NOT DELIVERED: $failed failed; intermediates kept in $WORK" >> "$MAIN_LOG"
elif [ "$PREPARE_ONLY" = 1 ]; then
  echo "PREPARED: --prepare-only, nothing built or delivered" >> "$MAIN_LOG"
fi
echo "exit $rc" >> "$MAIN_LOG"
grep -E "^(PASS|FAIL|NOT DELIVERED|PREPARED|delivered|unchanged|prepare skipped|hit|miss|npm ci in|deps.ok)" "$MAIN_LOG"
exit "$rc"
