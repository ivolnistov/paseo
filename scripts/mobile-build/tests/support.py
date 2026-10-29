"""Shared fixtures for the mobile-build script contract tests.

Everything runs in a fresh temp root with fake tools on PATH (and in PASEO_BUILD_BIN_DIR). Nothing here
installs packages, runs Gradle, signs, touches a device or the daemon, or writes outside the temp root.
HOME points into the temp root so a script default can never reach the real ~/.npm, ~/.gradle or keystore.
"""
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import zipfile

TESTS = pathlib.Path(__file__).resolve().parent
HELPERS = TESTS.parent
REPO = HELPERS.parent.parent
BUILD_APK = REPO / "scripts/build-apk.sh"
UPLOAD = REPO / "scripts/upload-nextcloud.sh"
DEPS_STATE = HELPERS / "deps-state.sh"
LOCK = HELPERS / "lock.sh"
VERIFY_APK = HELPERS / "verify_apk.py"

EXPECTED_CERT = "0226bc7d8f756b50c0bfddd40a3e942086b38a7354fd2ba5fdda713c7aca5ec0"
STOREPASS = "not-a-real-storepass-7f3a"
REAL_NODE = shutil.which("node") or "/usr/bin/false"

HERMES_EXECUTOR_PATH = "ReactAndroid/src/main/java/com/facebook/hermes/reactexecutor/HermesExecutor.kt"

# Shape of react-native 0.86.3 HermesExecutor.kt: the standalone engine (hermesvm) and the merged executor
# (hermes_executor) are both loaded by name, but only the engine ships as its own .so.
HERMES_EXECUTOR_RN086 = """package com.facebook.hermes.reactexecutor

import com.facebook.soloader.SoLoader

public class HermesExecutor internal constructor(enableDebugger: Boolean, debuggerName: String) :
    JavaScriptExecutor(initHybridDefaultConfig(enableDebugger, debuggerName)) {
  public companion object {
    private var mode: String? = null

    @JvmStatic
    public fun loadLibrary() {
      if (mode == null) {
        // libhermesvm must be loaded explicitly to invoke its JNI_OnLoad.
        SoLoader.loadLibrary("hermesvm")
        SoLoader.loadLibrary("hermes_executor")
        // libhermes_executor is built differently for Debug & Release so we load the proper mode.
        mode = if (ReactBuildConfig.DEBUG) "Debug" else "Release"
      }
    }
  }
}
"""

# Pre-0.8x shape: the engine was libhermes.so.
HERMES_EXECUTOR_LEGACY = HERMES_EXECUTOR_RN086.replace('loadLibrary("hermesvm")', 'loadLibrary("hermes")').replace(
    "libhermesvm must", "libhermes must")

# The SDK pins of react-native 0.86.3's node_modules/react-native/gradle/libs.versions.toml.
RN_CATALOG = 'minSdk = "24"\ntargetSdk = "36"\ncompileSdk = "36"\nbuildTools = "36.0.0"\nndkVersion = "27.1.12297006"\n'
RN_CATALOG_PATH = "react-native/gradle/libs.versions.toml"

# RN merges these into libreactnative.so / other archives; their presence is not required as standalone files.
COMMON_LIBS = ["libreactnative.so", "libjsi.so", "libc++_shared.so", "libfbjni.so", "libhermestooling.so"]


def write(path, text, mode=0o644):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_apk(path, libs, abi="arm64-v8a", extra=None):
    """A real zip with the entry names an APK verifier inspects. Content is irrelevant to these gates."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [("AndroidManifest.xml", b"\x03\x00\x08\x00"), ("classes.dex", b"dex\n035\x00"),
               ("assets/index.android.bundle", bytes.fromhex("c61fbc03c103191f") + b"\x00" * 24)]
    entries += [(lib if "/" in lib else f"lib/{abi}/{lib}", b"\x7fELF") for lib in libs]
    entries += list((extra or {}).items())
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            # Fixed timestamps keep the bytes, and so the sha256, identical across rebuilds of a fixture.
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return path


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def live_process():
    """A process that stays alive until the caller kills it; its pid is a realistic live lock owner."""
    return subprocess.Popen(["sleep", "300"])


FAKE_NODE = """#!/bin/sh
if [ "$1" = "-v" ] || [ "$1" = "--version" ]; then echo "${STUB_NODE_VERSION:-v26.12.0}"; exit 0; fi
exec "%s" "$@"
""" % REAL_NODE

# npm ci behaviours: ok, fail (partial output, exit 1), kill (partial output, SIGKILL), nolock (exit 0 without
# node_modules/.package-lock.json), mutate (exit 0 after rewriting package-lock.json).
FAKE_NPM = r"""#!/bin/sh
echo "npm $* @$PWD" >> "$STUB_CALLS"
case "$*" in
  *"run build:app-deps"*)  # builds the workspace packages the app imports; writes their entry outputs
    p="$PWD"; [ "$1" = --prefix ] && p="$2"
    mkdir -p "$p/packages/protocol/dist" && echo "module.exports = {};" > "$p/packages/protocol/dist/index.js"
    exit 0 ;;
esac
case "$1" in
  -v|--version) echo "${STUB_NPM_VERSION:-11.20.0}"; exit 0 ;;
  ci)
    rm -rf node_modules
    mkdir -p node_modules/partial
    case "${STUB_NPM_CI:-ok}" in
      fail) exit 1 ;;
      kill) kill -9 $$ ;;
      nolock) exit 0 ;;
      mutate) echo '{"mutated": true}' > package-lock.json; cp package-lock.json node_modules/.package-lock.json; exit 0 ;;
    esac
    cp package-lock.json node_modules/.package-lock.json
    mkdir -p node_modules/react-native/ReactAndroid/src/main/java/com/facebook/hermes/reactexecutor
    cp "$STUB_HERMES_EXECUTOR" node_modules/react-native/ReactAndroid/src/main/java/com/facebook/hermes/reactexecutor/HermesExecutor.kt
    echo '{"version": "0.86.3"}' > node_modules/react-native/package.json
    mkdir -p node_modules/expo && echo '{"version": "57.0.30"}' > node_modules/expo/package.json
    # The installed React Native version catalog (SDK pins): RN 0.86.3's by default, or the test's.
    mkdir -p node_modules/react-native/gradle
    if [ -n "${STUB_RN_CATALOG:-}" ]; then
      cp "$STUB_RN_CATALOG" node_modules/react-native/gradle/libs.versions.toml
    else
      printf 'minSdk = "24"\ntargetSdk = "36"\ncompileSdk = "36"\nbuildTools = "36.0.0"\nndkVersion = "27.1.12297006"\n' \
        > node_modules/react-native/gradle/libs.versions.toml
    fi
    # npm workspaces link each workspace package into node_modules.
    if [ "${STUB_WORKSPACE_LINKS:-0}" = 1 ]; then
      for d in packages/*; do
        n=$(sed -n 's/.*"name": *"@getpaseo\/\([^"]*\)".*/\1/p' "$d/package.json" 2>/dev/null)
        [ -n "$n" ] && mkdir -p node_modules/@getpaseo && ln -sfn "../../$d" "node_modules/@getpaseo/$n"
      done
    fi
    exit 0 ;;
esac
exit 0
"""

# Expo prebuild stub: writes a minimal generated project with a gradlew that records its arguments.
FAKE_NPX = r"""#!/bin/sh
echo "npx $* @$PWD" >> "$STUB_CALLS"
A="$PWD/android"
mkdir -p "$A/app/src/main/java/sh/paseo" "$A/gradle/wrapper"
printf "        applicationId 'sh.paseo'\n        versionCode 424242\n        versionName \"7.8.9\"\napply plugin: 'com.google.gms.google-services'\n" > "$A/app/build.gradle"
echo "classpath 'com.google.gms:google-services:4.4.4'" > "$A/build.gradle"
for f in settings.gradle gradle.properties gradle/wrapper/gradle-wrapper.properties app/src/main/AndroidManifest.xml \
         app/src/main/java/sh/paseo/MainActivity.kt app/src/main/java/sh/paseo/MainApplication.kt; do echo "$f" > "$A/$f"; done
[ -n "$GOOGLE_SERVICES_FILE_PROD" ] && cp "$GOOGLE_SERVICES_FILE_PROD" "$A/app/google-services.json"
cat > "$A/gradlew" <<'EOF'
#!/bin/sh
echo "gradlew $*" >> "$STUB_CALLS"
echo "BUILD FAILED in 1s"
exit "${STUB_GRADLE_EXIT:-1}"
EOF
chmod +x "$A/gradlew"
exit 0
"""

FAKE_KEYTOOL = r"""#!/bin/sh
echo "keytool $*" >> "$STUB_CALLS"
cert="${STUB_CERT:-%s}"
colon=$(echo "$cert" | tr 'a-f' 'A-F' | sed 's/../&:/g; s/:$//')
echo "Alias name: paseo-local-release"
echo "Certificate fingerprints:"
echo "	 SHA256: $colon"
exit 0
""" % EXPECTED_CERT

FAKE_JAVA = """#!/bin/sh
echo 'openjdk version "17.0.21" 2026-10-20' >&2
exit 0
"""

FAIL_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_CALLS"
exit 9
"""


class ToolTestCase(unittest.TestCase):
    """Temp root, fake bin dir and a scrubbed environment per test."""

    required = ()

    def setUp(self):
        for path in self.required:
            self.assertTrue(path.exists(), f"contract under test is not implemented yet: {path}")
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="paseo-mobile-build-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "fakebin"
        write(self.bin / "node", FAKE_NODE, 0o755)
        write(self.bin / "npm", FAKE_NPM, 0o755)
        write(self.bin / "npx", FAKE_NPX, 0o755)
        write(self.bin / "keytool", FAKE_KEYTOOL, 0o755)
        for tool in ("gradlew", "apksigner", "zipalign", "aapt2", "dexdump", "adb"):
            write(self.bin / tool, FAIL_TOOL, 0o755)
        self.calls = self.root / "calls.log"
        self.calls.touch()
        self.hermes_fixture = write(self.root / "fixtures/HermesExecutor.kt", HERMES_EXECUTOR_RN086)

    def env(self, **extra):
        drop = ("PASEO_", "EXPO_", "EAS_", "STUB_", "GOOGLE_SERVICES", "npm_")
        env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
        for k in ("APP_VARIANT", "NODE_ENV", "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
            env.pop(k, None)
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            PASEO_BUILD_BIN_DIR=str(self.bin),
            PASEO_BUILD_NOW="2026-10-24T00:00:00Z",
            STUB_CALLS=str(self.calls),
            STUB_HERMES_EXECUTOR=str(self.hermes_fixture),
        )
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run_cmd(self, argv, cwd=None, timeout=120, **env_extra):
        p = subprocess.run([str(a) for a in argv], env=self.env(**env_extra), capture_output=True, text=True,
                           cwd=str(cwd or self.root), timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def calls_matching(self, prefix):
        return [line for line in self.calls.read_text().splitlines() if line.startswith(prefix)]

    def snapshot(self, base):
        """Every path under base with its type, size and mtime: proves 'nothing written'."""
        base = pathlib.Path(base)
        if not base.exists():
            return None
        out = {}
        for p in sorted(base.rglob("*")):
            st = p.lstat()
            out[str(p.relative_to(base))] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
        return out


class SourceFixture:
    """A committed git checkout shaped like the app repo, with an installed node_modules (ignored)."""

    def __init__(self, root, hermes=HERMES_EXECUTOR_RN086):
        self.src = root / "src"
        s = self.src
        write(s / ".gitignore", "node_modules/\n.dev/\npackages/app/android/\n")
        write(s / "package.json", json.dumps({"name": "paseo", "private": True,
                                              "scripts": {"postinstall": "node scripts/postinstall-patches.mjs"}}))
        write(s / "package-lock.json", '{"lockfileVersion": 3, "fixture": 1}\n')
        write(s / "scripts/postinstall-patches.mjs", "// applies patches/\n")
        write(s / "patches/example+1.0.0.patch", "--- a\n+++ b\n")
        write(s / "packages/app/package.json", json.dumps({"name": "@getpaseo/app", "version": "0.13.0-beta.2"}))
        # Deliberately not the real mapping: the build must take versionName/code from this file, not hardcode them.
        write(s / "packages/app/native-release-version.js",
              "function getNativeReleaseVersion(version) {\n"
              "  return { appVersion: '7.8.9', androidVersionCode: 424242, iosBuildNumber: '1' };\n}\n"
              "module.exports = { getNativeReleaseVersion };\n")
        write(s / "packages/app/app.config.js", "module.exports = () => ({ name: 'Paseo' });\n")
        git(s, "init", "-q")
        git(s, "add", "-A")
        git(s, "commit", "-qm", "fixture")
        self.install(hermes)

    def install(self, hermes):
        nm = self.src / "node_modules"
        write(nm / ".package-lock.json", (self.src / "package-lock.json").read_text())
        write(nm / "react-native/package.json", '{"version": "0.86.3"}\n')
        write(nm / "react-native" / HERMES_EXECUTOR_PATH, hermes)
        write(nm / RN_CATALOG_PATH, RN_CATALOG)
        write(nm / "expo/package.json", '{"version": "57.0.30"}\n')

    @property
    def head(self):
        return git(self.src, "rev-parse", "HEAD")

    def status(self):
        return git(self.src, "status", "--porcelain")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()
