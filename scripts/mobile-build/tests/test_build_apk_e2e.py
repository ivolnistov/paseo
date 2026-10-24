"""build-apk.sh end to end with fake build tools: successful signed delivery, prebuild cache decisions, the
generated-project plugin gates, the APK verification gates, and dependency receipt adoption.

Nothing real is built or signed. The fake prebuild writes a project shaped like the real beta.1 generated output
(MainActivity dispatchKeyEvent override, fold configChanges); the fake gradlew copies a fixture APK into place; the
fake SDK tools answer in their real output formats. Each STUB_* knob injects exactly one fault.
"""
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import (BUILD_APK, COMMON_LIBS, DEPS_STATE, EXPECTED_CERT, STOREPASS, SourceFixture, git,  # noqa: E402
                     make_apk, sha256, write)

GOOGLE_SERVICES = {
    "project_info": {"project_number": "123456", "project_id": "paseo-fixture", "storage_bucket": "paseo-fixture.appspot.com"},
    "client": [{"client_info": {"mobilesdk_app_id": "1:123456:android:fixture", "android_client_info": {"package_name": "sh.paseo"}},
                "api_key": [{"current_key": "fixture-api-key"}]},
               {"client_info": {"mobilesdk_app_id": "1:123456:android:fixture", "android_client_info": {"package_name": "sh.paseo.debug"}},
                "api_key": [{"current_key": "fixture-api-key"}]}],
}

DEX_CLASSES = [
    "expo/modules/ExpoModulesPackageList",
    "sh/paseo/hardwarekeyboard/PaseoHardwareKeyboardModule",
    "sh/paseo/hardwarekeyboard/PaseoHardwareKeyboardDispatcher",
    "sh/paseo/hardwarekeyboard/SubmitKeySequence",
    "sh/paseo/screengeometry/PaseoScreenGeometryModule",
    "expo/modules/screenorientation/ScreenOrientationModule",
]
REGISTRY = [
    "sh/paseo/hardwarekeyboard/PaseoHardwareKeyboardModule",
    "sh/paseo/screengeometry/PaseoScreenGeometryModule",
    "expo/modules/screenorientation/ScreenOrientationModule",
]

# Expo prebuild stand-in. The MainActivity and manifest line copy the real generated beta.1 shapes.
FAKE_NPX = r"""#!/bin/sh
echo "npx $* @$PWD" >> "$STUB_CALLS"
echo "env-npx APP_VARIANT=$APP_VARIANT PROD=${GOOGLE_SERVICES_FILE_PROD:+set} DEBUG=${GOOGLE_SERVICES_FILE_DEBUG:+set}" >> "$STUB_CALLS"
A="$PWD/android"
# Expo places the generated activity in the Java package path of the app's package id.
P="$A/app/src/main/java/$(echo "${STUB_PACKAGE:-sh.paseo}" | tr . /)"
mkdir -p "$P" "$A/gradle/wrapper"
printf "        applicationId '${STUB_PACKAGE:-sh.paseo}'\n        versionCode 424242\n        versionName \"7.8.9\"\napply plugin: 'com.google.gms.google-services'\n" > "$A/app/build.gradle"
echo "classpath 'com.google.gms:google-services:4.4.4'" > "$A/build.gradle"
for f in settings.gradle gradle.properties; do echo "$f" > "$A/$f"; done
echo "MainApplication" > "$P/MainApplication.kt"
echo "distributionUrl=https\://services.gradle.org/distributions/gradle-9.3.1-bin.zip" > "$A/gradle/wrapper/gradle-wrapper.properties"
echo "distributionSha256Sum=${STUB_GRADLE_DIST_SHA:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" >> "$A/gradle/wrapper/gradle-wrapper.properties"
printf 'wrapper jar fixture' > "$A/gradle/wrapper/gradle-wrapper.jar"
changes="${STUB_CONFIG_CHANGES:-keyboard|keyboardHidden|orientation|screenSize|screenLayout|uiMode|smallestScreenSize}"
orientation="${STUB_SCREEN_ORIENTATION:-unspecified}"
cat > "$A/app/src/main/AndroidManifest.xml" <<EOF
<manifest>
    <activity android:name=".MainActivity" android:configChanges="$changes" android:launchMode="singleTask" android:exported="true" android:screenOrientation="$orientation">
</manifest>
EOF
if [ "${STUB_NO_DISPATCH:-0}" = 1 ]; then
  echo "class MainActivity : ReactActivity() {}" > "$P/MainActivity.kt"
else
  cat > "$P/MainActivity.kt" <<'EOF'
class MainActivity : ReactActivity() {
  // Hardware keyboard Enter reaches the composer's Enter rules before ReactEditText.
  override fun dispatchKeyEvent(event: android.view.KeyEvent): Boolean {
    if (sh.paseo.hardwarekeyboard.PaseoHardwareKeyboardDispatcher.dispatchKeyEvent(this, event)) return true
    return super.dispatchKeyEvent(event)
  }
}
EOF
fi
# app.config.js: development reads GOOGLE_SERVICES_FILE_DEBUG, production GOOGLE_SERVICES_FILE_PROD.
case "$APP_VARIANT" in development) gs="$GOOGLE_SERVICES_FILE_DEBUG" ;; *) gs="$GOOGLE_SERVICES_FILE_PROD" ;; esac
[ -n "$gs" ] && cp "$gs" "$A/app/google-services.json"
cat > "$A/gradlew" <<'EOF'
#!/bin/sh
echo "gradlew $*" >> "$STUB_CALLS"
# Hold the build until the test releases it, so a concurrent run can be observed.
if [ -n "${STUB_GRADLE_HOLD:-}" ]; then while [ ! -e "$STUB_GRADLE_HOLD" ]; do sleep 0.1; done; fi
echo "env-gradlew APP_VARIANT=$APP_VARIANT PROD=${GOOGLE_SERVICES_FILE_PROD:+set} DEBUG=${GOOGLE_SERVICES_FILE_DEBUG:+set}" >> "$STUB_CALLS"
mkdir -p app/build/outputs/apk/release
cp "$STUB_APK" app/build/outputs/apk/release/app-release.apk
echo "> Task :app:assembleRelease"
echo "BUILD SUCCESSFUL in 1s"
EOF
chmod +x "$A/gradlew"
"""

FAKE_JAVA = """#!/bin/sh
echo "openjdk version \\"${STUB_JAVA_VERSION:-17.0.21}\\" 2026-10-20" >&2
"""

FAKE_ZIPALIGN = r"""#!/bin/sh
echo "zipalign $*" >> "$STUB_CALLS"
if [ "$1" = "-c" ]; then echo "Verification succesful"; exit "${STUB_ZIPALIGN_CHECK_EXIT:-0}"; fi
eval "in=\${$(($# - 1))}"; eval "out=\${$#}"
cp "$in" "$out"
"""

FAKE_APKSIGNER = r"""#!/bin/sh
echo "apksigner $*" >> "$STUB_CALLS"
if [ "$1" = sign ]; then
  [ -z "${STUB_SIGN_EXIT:-}" ] || exit "$STUB_SIGN_EXIT"
  out=""; prev=""; for a in "$@"; do [ "$prev" = "--out" ] && out=$a; prev=$a; last=$a; done
  cp "$last" "$out"; exit 0
fi
echo "Verifies"
echo "Verified using v1 scheme (JAR signing): false"
echo "Verified using v2 scheme (APK Signature Scheme v2): true"
echo "Verified using v3 scheme (APK Signature Scheme v3): true"
echo "Number of signers: 1"
echo "Signer #1 certificate SHA-256 digest: ${STUB_SIGNER_CERT}"
"""

FAKE_AAPT2 = r"""#!/bin/sh
echo "aapt2 $*" >> "$STUB_CALLS"
[ -z "${STUB_AAPT2_EXIT:-}" ] || exit "$STUB_AAPT2_EXIT"
case "$2" in
  badging)
    echo "package: name='${STUB_PACKAGE:-sh.paseo}' versionCode='${STUB_BADGING_CODE:-424242}' versionName='7.8.9' platformBuildVersionName='16'"
    echo "native-code: 'arm64-v8a'" ;;
  resources) cat "$STUB_RESOURCES" ;;
esac
"""

FAKE_DEXDUMP = r"""#!/bin/sh
for c in $STUB_DEX_CLASSES; do
  [ "$c" = expo/modules/ExpoModulesPackageList ] && continue
  echo "  Class descriptor  : 'L$c;'"
done
echo "  Class descriptor  : 'Lexpo/modules/ExpoModulesPackageList;'"
for r in $STUB_REGISTRY; do echo "      0000: const-class v1, L$r; // type@0001"; done
"""


class E2ETest(support.ToolTestCase):
    required = (BUILD_APK, DEPS_STATE, support.LOCK, support.VERIFY_APK)

    def setUp(self):
        super().setUp()
        write(self.bin / "npx", FAKE_NPX, 0o755)
        self.fx = SourceFixture(self.root)
        s = self.fx.src
        # The plugin and module declarations that switch on the generated-project and DEX/registry gates.
        write(s / "packages/app/modules/paseo-hardware-keyboard/app.plugin.js", "module.exports = (c) => c;\n")
        write(s / "packages/app/modules/paseo-hardware-keyboard/android/build.gradle", "// module\n")
        write(s / "packages/app/modules/paseo-screen-geometry/android/build.gradle", "// module\n")
        write(s / "packages/app/plugins/with-android-fold-configuration.js", "module.exports = (c) => c;\n")
        write(s / "packages/app/app.config.js", "module.exports = () => ({ name: 'Paseo', orientation: \"default\" });\n")
        write(s / "packages/app/package.json", json.dumps({"name": "@getpaseo/app", "version": "0.13.0-beta.2",
                                                          "dependencies": {"expo-screen-orientation": "~57.0.3"}}))
        git(s, "add", "-A")
        git(s, "commit", "-qm", "declare native modules")
        r = self.root
        self.jdk = r / "jdk"
        write(self.jdk / "bin/java", FAKE_JAVA, 0o755)
        self.sdk = r / "sdk"
        bt = self.sdk / "build-tools/36.0.0"
        write(bt / "zipalign", FAKE_ZIPALIGN, 0o755)
        write(bt / "apksigner", FAKE_APKSIGNER, 0o755)
        write(bt / "aapt2", FAKE_AAPT2, 0o755)
        write(bt / "dexdump", FAKE_DEXDUMP, 0o755)
        write(self.sdk / "ndk/27.1.12297006/source.properties", "Pkg.Revision = 27.1.12297006\n")
        write(self.sdk / "platforms/android-36/android.jar", "jar\n")
        self.keys = r / "keys"
        write(self.keys / "release.p12", "not a real keystore\n", 0o600)
        write(self.keys / "storepass", STOREPASS + "\n", 0o600)
        self.gs = write(r / "inputs/google-services.json", json.dumps(GOOGLE_SERVICES), 0o600)
        self.resources = write(r / "fixtures/resources.txt", self.resources_for(GOOGLE_SERVICES))
        self.apk = make_apk(r / "fixtures/app-release.apk", ["libhermesvm.so", *COMMON_LIBS])
        self.state = r / "state"
        self.runs = 0

    @staticmethod
    def resources_for(gs):
        info, client = gs["project_info"], gs["client"][0]
        key = client["api_key"][0]["current_key"]
        values = {"gcm_defaultSenderId": info["project_number"], "google_api_key": key,
                  "google_app_id": client["client_info"]["mobilesdk_app_id"], "google_crash_reporting_api_key": key,
                  "google_storage_bucket": info["storage_bucket"], "project_id": info["project_id"]}
        lines = ["Package name=sh.paseo id=7f", "  type string id=13 entryCount=6"]
        for i, (name, value) in enumerate(values.items()):
            lines += [f"    resource 0x7f13{i:04x} string/{name}", f'      () "{value}"']
        return "\n".join(lines) + "\n"

    def build_args(self, *extra, out=None, variant="production", package="sh.paseo", state=None):
        return ["bash", BUILD_APK, "--src", self.fx.src, "--expected-sha", self.fx.head, "--version", "0.13.0-beta.2",
                "--variant", variant, "--package", package, "--abi", "arm64-v8a", "--google-services", self.gs,
                "--keystore", self.keys, "--expected-cert", EXPECTED_CERT, "--out", out or self.root / "out",
                "--state-dir", state or self.state, "--java-home", self.jdk, "--android-home", self.sdk, *extra]

    def stubs(self, **env):
        stub = dict(STUB_APK=self.apk, STUB_RESOURCES=self.resources, STUB_SIGNER_CERT=EXPECTED_CERT,
                    STUB_DEX_CLASSES=" ".join(DEX_CLASSES), STUB_REGISTRY=" ".join(REGISTRY))
        stub.update(env)
        return stub

    def build(self, *extra, out=None, variant="production", package="sh.paseo", **env):
        self.runs += 1
        out = out or self.root / "out"
        stub = dict(STUB_APK=self.apk, STUB_RESOURCES=self.resources, STUB_SIGNER_CERT=EXPECTED_CERT,
                    STUB_DEX_CLASSES=" ".join(DEX_CLASSES), STUB_REGISTRY=" ".join(REGISTRY))
        stub.update(env)
        args = ["bash", BUILD_APK, "--src", self.fx.src, "--expected-sha", self.fx.head, "--version", "0.13.0-beta.2",
                "--variant", variant, "--package", package, "--abi", "arm64-v8a", "--google-services", self.gs,
                "--keystore", self.keys, "--expected-cert", EXPECTED_CERT, "--out", out, "--state-dir", self.state,
                "--java-home", self.jdk, "--android-home", self.sdk, *extra]
        return self.run_cmd(args, timeout=300, **stub)

    def count(self, prefix):
        return len(self.calls_matching(prefix))

    def delivered(self, out=None, package="sh.paseo"):
        out = out or self.root / "out"
        name = f"paseo-v0.13.0-beta.2-{package}-arm64-v8a.apk"
        return out / name, out / f"{name}.sha256", out / f"{name}.build-manifest.json"

    def assert_not_delivered(self, out):
        self.assertFalse(list(out.glob("*.apk")) if out.exists() else [], "nothing delivered")

    def test_successful_build_delivers_bound_artifacts(self):
        patch = "df3b72d16c1f15661c5b5fccf1d60d410cec4201cd1ebec9626f9a61e40ede6c"
        port = "ab" * 32
        provenance = ["--provenance", f"client.patch={patch.upper()}", "--provenance", f"beta1-port.patch={port}",
                      "--provenance", f"client.patch={patch}"]
        rc, out = self.build("--with-jvm-tests", *provenance)
        self.assertEqual(rc, 0, out)
        apk, sidecar, manifest = self.delivered()
        self.assertEqual(apk.read_bytes(), self.apk.read_bytes())
        self.assertEqual(sidecar.read_text().split(), [sha256(apk), apk.name])
        m = json.loads(manifest.read_text())
        self.assertEqual(m["artifact"], {"file": apk.name, "sha256": sha256(apk), "size": apk.stat().st_size})
        self.assertEqual(m["source"], {"sha": self.fx.head, "tree": git(self.fx.src, "rev-parse", "HEAD^{tree}")})
        self.assertEqual((m["version"], m["versionName"], m["versionCode"]), ("0.13.0-beta.2", "7.8.9", 424242))
        self.assertEqual((m["variant"], m["package"], m["abi"], m["signerCertSha256"]),
                         ("production", "sh.paseo", "arm64-v8a", EXPECTED_CERT))
        self.assertEqual(m["inputs"]["packageLockSha256"], sha256(self.fx.src / "package-lock.json"))
        self.assertEqual(m["inputs"]["googleServicesSha256"], sha256(self.gs))
        self.assertTrue(all(m["cache"][k] for k in ("dependencies", "prebuild", "jsBundle")), m["cache"])
        self.assertEqual(m["provenance"], {"client.patch": patch, "beta1-port.patch": port})
        wrapper = self.fx.src / "packages/app/android/gradle/wrapper"
        self.assertEqual(m["toolchain"]["gradleWrapper"], {
            "distributionUrl": "https://services.gradle.org/distributions/gradle-9.3.1-bin.zip",
            "distributionSha256Sum": "0123456789abcdef" * 4,
            "propertiesSha256": sha256(wrapper / "gradle-wrapper.properties"),
            "wrapperJarSha256": sha256(wrapper / "gradle-wrapper.jar"),
        })
        gradle = self.calls_matching("gradlew")
        self.assertEqual(len(gradle), 1)
        self.assertIn(":app:createBundleReleaseJsAndAssets --rerun", gradle[0], "first build forces a fresh bundle")
        self.assertIn(":paseo-hardware-keyboard:testReleaseUnitTest", gradle[0])
        self.assertTrue(any("--ks-pass file:" in c for c in self.calls_matching("apksigner sign")))
        self.assertNotIn(STOREPASS, self.calls.read_text() + out)
        self.assertNotIn("fixture-api-key", out)
        self.assertEqual(self.fx.status(), "")
        self.assertFalse((self.state / "build.lock").exists())

        # Unchanged inputs: deps reused, preparation skipped, no bundle rerun; identical delivery is a no-op.
        before = {p: p.stat().st_ino for p in (apk, sidecar, manifest)}
        rc, out = self.build("--with-jvm-tests", *provenance)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.count("npm ci"), self.count("npx")), (1, 1), out)
        self.assertNotIn("--rerun", self.calls_matching("gradlew")[-1])
        self.assertEqual({p: p.stat().st_ino for p in (apk, sidecar, manifest)}, before, "delivered files untouched")

        # The dry-run preview of the same warm inputs must report what the real run did: prebuild skipped.
        rc, out = self.build("--dry-run", "--with-jvm-tests", *provenance)
        self.assertEqual(rc, 0, out)
        prepare = [line for line in out.splitlines() if line.strip().startswith("prepare ")]
        self.assertEqual(len(prepare), 1, out)
        self.assertIn("skip", prepare[0], "the warm preview skips prebuild")
        self.assertNotIn("changed", prepare[0], "no input changed between the real run and its preview")

    def test_profile_switch_invalidates_prebuild_and_bundle(self):
        # app.config.js and metro.config.cjs read the switch, so a change must regenerate the project and bundle.
        # (PASEO_FDROID_BUILD is refused in preflight: the tool does not produce F-Droid builds.)
        self.assertEqual(self.build()[0], 0)
        for i, switch in enumerate(("PASEO_PROFILE_BUILD",), start=2):
            with self.subTest(switch):
                rc, out = self.build(out=self.root / f"out-{switch}", **{switch: "1"})
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.count("npx"), i, f"{switch}=1 must rerun prebuild\n{out}")
                self.assertIn(":app:createBundleReleaseJsAndAssets --rerun", self.calls_matching("gradlew")[-1],
                              f"{switch}=1 must rerun the JS bundle task")

    def test_identical_apk_with_conflicting_manifest_is_refused(self):
        patch = "ab" * 32
        rc, out = self.build("--provenance", f"client.patch={patch}")
        self.assertEqual(rc, 0, out)
        apk, sidecar, manifest = self.delivered()
        finals = lambda: {p.name: (p.stat().st_ino, p.read_bytes()) for p in self.root.joinpath("out").glob("paseo-*")}  # noqa: E731
        original = finals()

        # Same APK bytes, different immutable provenance: the delivered receipt would no longer describe this run.
        rc, out = self.build("--provenance", "client.patch=" + "cd" * 32)
        self.assertEqual(rc, 5, out)
        self.assertEqual(finals(), original, "a refusal changes no final file")

        # Same APK bytes, but the delivered manifest names another source: refuse rather than adopt it.
        recorded = json.loads(manifest.read_text())
        recorded["source"]["sha"] = "0" * 40
        manifest.write_text(json.dumps(recorded, indent=2) + "\n")
        conflicting = finals()
        rc, out = self.build("--provenance", f"client.patch={patch}")
        self.assertEqual(rc, 5, out)
        self.assertEqual(finals(), conflicting, "a refusal changes no final file")

    def test_development_variant_routes_debug_google_services(self):
        rc, out = self.build(variant="development", package="sh.paseo.debug", STUB_PACKAGE="sh.paseo.debug")
        self.assertEqual(rc, 0, out)
        for tool in ("env-npx", "env-gradlew"):
            lines = self.calls_matching(tool)
            self.assertTrue(lines, tool)
            self.assertTrue(all("APP_VARIANT=development" in l and "DEBUG=set" in l for l in lines), lines)
        self.assertTrue(self.delivered(package="sh.paseo.debug")[0].is_file())

    def test_watchdog_switch_invalidates_prebuild_and_bundle(self):
        # app.config.js adds the profileable plugin and extra.watchdogBuild; metro.config.cjs swaps modules.
        self.assertEqual(self.build()[0], 0)
        rc, out = self.build(out=self.root / "out-watchdog", PASEO_WATCHDOG_BUILD="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.count("npx"), 2, "PASEO_WATCHDOG_BUILD=1 must rerun prebuild\n" + out)
        self.assertIn(":app:createBundleReleaseJsAndAssets --rerun", self.calls_matching("gradlew")[-1])

    def test_failing_verifier_tool_exits_4(self):
        for i, (label, env) in enumerate((("apksigner sign", {"STUB_SIGN_EXIT": "9"}),
                                          ("aapt2", {"STUB_AAPT2_EXIT": "9"}))):
            with self.subTest(label):
                out = self.root / f"out-tool-{i}"
                rc, log = self.build(out=out, **env)
                self.assertEqual(rc, 4, f"{label}\n{log}")
                self.assert_not_delivered(out)

    def test_sdk_pins_come_from_the_catalog_installed_by_this_run(self):
        # Installed SDK besides the defaults: the catalog that npm ci installs pins these, not the fallbacks.
        bt = self.sdk / "build-tools/35.0.0"
        bt.mkdir(parents=True)
        for tool in ("zipalign", "apksigner", "aapt2", "dexdump"):
            shutil.copy2(self.sdk / "build-tools/36.0.0" / tool, bt / tool)
        write(self.sdk / "ndk/27.0.12077973/source.properties", "Pkg.Revision = 27.0.12077973\n")
        write(self.sdk / "platforms/android-35/android.jar", "jar\n")
        catalog = write(self.root / "fixtures/libs.versions.toml",
                        'compileSdk = "35"\ntargetSdk = "35"\nbuildTools = "35.0.0"\nndkVersion = "27.0.12077973"\n')
        (self.fx.src / "node_modules" / support.RN_CATALOG_PATH).unlink()  # cold: no catalog before this run's install
        rc, out = self.build(STUB_RN_CATALOG=catalog)
        self.assertEqual(rc, 0, out)
        toolchain = json.dumps(json.loads(self.delivered()[2].read_text())["toolchain"])
        for pin in ("35.0.0", "27.0.12077973", "android-35"):
            self.assertIn(pin, toolchain, "the manifest records the installed catalog's pins")
        self.assertNotIn("36.0.0", toolchain)

    def test_catalog_naming_a_missing_ndk_stops_before_prebuild(self):
        catalog = write(self.root / "fixtures/libs.versions.toml",
                        'compileSdk = "36"\nbuildTools = "36.0.0"\nndkVersion = "27.9.99999"\n')
        rc, out = self.build("--prepare-only", STUB_RN_CATALOG=catalog)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.count("npx"), 0, "no prebuild with an NDK the installed catalog requires but is missing")

    def test_missing_workspace_outputs_need_app_deps(self):
        s = self.fx.src
        write(s / ".gitignore", "node_modules/\n.dev/\npackages/app/android/\ndist/\n")
        write(s / "packages/protocol/package.json", json.dumps({"name": "@getpaseo/protocol", "main": "dist/index.js",
                                                                "exports": {".": {"default": "./dist/index.js"}}}))
        app = json.loads((s / "packages/app/package.json").read_text())
        app["dependencies"]["@getpaseo/protocol"] = "*"
        write(s / "packages/app/package.json", json.dumps(app))
        git(s, "add", "-A")
        git(s, "commit", "-qm", "app imports a workspace package")
        rc, out = self.build("--prepare-only", STUB_WORKSPACE_LINKS="1")
        self.assertEqual(rc, 3, out)
        self.assertIn("--with-app-deps", out, "the refusal names the flag that builds the outputs")
        self.assertEqual(self.count("npx"), 0, "refused before prebuild")
        rc, out = self.build("--prepare-only", "--with-app-deps", STUB_WORKSPACE_LINKS="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.count("npx"), 1)
        rc, out = self.build("--prepare-only", STUB_WORKSPACE_LINKS="1")
        self.assertEqual(rc, 0, "existing valid outputs need no flag and no rebuild\n" + out)

    def test_one_build_per_source_across_state_dirs(self):
        hold = self.root / "release-gradle"
        first = subprocess.Popen([str(a) for a in self.build_args(out=self.root / "out-a", state=self.root / "state-a")],
                                 env=self.env(**self.stubs(STUB_GRADLE_HOLD=hold)), cwd=str(self.root),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(first.stdout.close)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.assertTrue(support.wait_until(lambda: self.count("gradlew") == 1, timeout=180), "first build reached Gradle")
        calls = self.calls.read_text()
        rc, out = self.run_cmd(self.build_args(out=self.root / "out-b", state=self.root / "state-b"), timeout=300,
                               **self.stubs())
        self.assertEqual(rc, 3, "a second build of the same source is refused\n" + out)
        self.assertEqual(self.calls.read_text(), calls, "the refused build ran no tool")
        hold.touch()
        output, _ = first.communicate(timeout=300)
        self.assertEqual(first.returncode, 0, output)

    def test_differing_delivered_apk_is_refused(self):
        out = self.root / "out"
        apk, sidecar, manifest = self.delivered(out)
        write(apk, "an earlier, different artifact\n")
        rc, log = self.build(out=out)
        self.assertEqual(rc, 5, log)
        self.assertEqual(apk.read_text(), "an earlier, different artifact\n")
        self.assertFalse(sidecar.exists() or manifest.exists(), "a refused delivery writes none of the set")

    def test_prebuild_cache_hit_and_invalidation(self):
        self.assertEqual(self.build("--prepare-only")[0], 0)
        self.assertEqual(self.count("npx"), 1)
        rc, out = self.build("--prepare-only")
        self.assertEqual((rc, self.count("npx")), (0, 1), "identical inputs skip prebuild\n" + out)

        def firebase_change():
            gs = json.loads(json.dumps(GOOGLE_SERVICES))
            gs["client"][0]["client_info"]["mobilesdk_app_id"] = "1:123456:android:other"
            self.gs.write_text(json.dumps(gs))

        def source_change():
            write(self.fx.src / "packages/app/src/feature.ts", "export const changed = 1;\n")
            git(self.fx.src, "add", "-A")
            git(self.fx.src, "commit", "-qm", "change")

        cases = [
            ("firebase input", firebase_change, {}, {}),
            ("source commit", source_change, {}, {}),
            ("toolchain (JDK build)", lambda: None, {"STUB_JAVA_VERSION": "17.0.22"}, {}),
            ("variant", lambda: None, {"STUB_JAVA_VERSION": "17.0.22", "STUB_PACKAGE": "sh.paseo.debug"},
             {"variant": "development", "package": "sh.paseo.debug"}),
        ]
        expected = 1
        for label, mutate, env, kw in cases:
            with self.subTest(label):
                mutate()
                rc, out = self.build("--prepare-only", **kw, **env)
                expected += 1
                self.assertEqual((rc, self.count("npx")), (0, expected), f"{label} reruns prebuild\n{out}")
                rc, out = self.build("--prepare-only", **kw, **env)
                self.assertEqual((rc, self.count("npx")), (0, expected), f"{label}: repeat is a hit\n{out}")
        self.assertTrue(all("--clean" not in c for c in self.calls_matching("npx")))

    def test_generated_project_gates(self):
        cases = [
            ("no keyboard dispatch", {"STUB_NO_DISPATCH": "1"}),
            ("no fold configChanges", {"STUB_CONFIG_CHANGES": "keyboard|keyboardHidden|orientation|screenSize"}),
            ("portrait lock with orientation default", {"STUB_SCREEN_ORIENTATION": "portrait"}),
        ]
        for label, env in cases:
            with self.subTest(label):
                rc, out = self.build("--prepare-only", "--force-prepare", **env)
                self.assertEqual(rc, 4, f"{label}\n{out}")
                self.assertFalse((self.state / "prepare.stamp").exists(), f"{label}: failed preparation leaves no stamp")
                self.assertEqual(self.calls_matching("gradlew"), [])
        rc, out = self.build("--prepare-only", "--force-prepare")
        self.assertEqual(rc, 0, out)

    def test_apk_verification_gates(self):
        missing_class = [c for c in DEX_CLASSES if not c.endswith("SubmitKeySequence")]
        missing_ref = [r for r in REGISTRY if "screenorientation" not in r]
        cases = [
            ("wrong signer", {"STUB_SIGNER_CERT": "f" * 64}),
            ("version mismatch", {"STUB_BADGING_CODE": "13000"}),
            ("missing DEX class", {"STUB_DEX_CLASSES": " ".join(missing_class)}),
            ("missing registry ref", {"STUB_REGISTRY": " ".join(missing_ref)}),
            ("foreign ABI", {"STUB_APK": make_apk(self.root / "fixtures/x86.apk", ["libhermesvm.so", *COMMON_LIBS,
                                                                                    "lib/x86_64/libreactnative.so"])}),
        ]
        for i, (label, env) in enumerate(cases):
            with self.subTest(label):
                out = self.root / f"out-{i}"
                rc, log = self.build(out=out, **env)
                self.assertEqual(rc, 4, f"{label}\n{log}")
                self.assertIn("NOT DELIVERED", (sorted(out.glob("work-*"))[-1] / "logs/build.log").read_text())
                self.assert_not_delivered(out)
        rc, log = self.build(out=self.root / "out-ok")
        self.assertEqual(rc, 0, log)
        manifest = json.loads(self.delivered(self.root / "out-ok")[2].read_text())
        self.assertEqual(manifest["provenance"], {}, "provenance is optional and empty by default")

    def receipt(self, **override):
        fp = subprocess.run(["bash", str(DEPS_STATE), "fingerprint", "--src", str(self.fx.src), "--state-dir",
                             str(self.state)], env=self.env(), capture_output=True, text=True, check=True).stdout
        cur = dict(line.split("=", 1) for line in fp.splitlines() if "=" in line)
        data = {
            "status": "success", "exit": 0, "command": "npm ci --prefer-offline", "worktree": str(self.fx.src),
            "lockSha256Before": cur["lock"], "lockSha256After": cur["lock"], "installedLockSha256": cur["installed_lock"],
            "workspacePackageJsonsSha256": cur["workspace_package_jsons"], "patchesDirSha256": cur["patches"],
            "postinstallScriptSha256": cur["postinstall"], "node": cur["node"], "npm": cur["npm"],
            "platform": cur["platform"], "arch": cur["arch"],
            "postconditions": {"nodeModulesRealDir": True, "hiddenLockPresent": True, "lockUnchanged": True},
        }
        data.update(override)
        return write(self.root / "receipts" / f"r{self.runs}-{len(override)}.json", json.dumps(data))

    def test_deps_receipt_adoption(self):
        bad = [
            ("failed install", {"status": "failed"}),
            ("other worktree", {"worktree": str(self.root / "elsewhere")}),
            ("lock changed", {"lockSha256After": "0" * 64}),
            ("installed lock", {"installedLockSha256": "0" * 64}),
            ("node version", {"node": "v24.0.0"}),
            ("postconditions", {"postconditions": {"nodeModulesRealDir": True, "hiddenLockPresent": False,
                                                   "lockUnchanged": True}}),
        ]
        for label, override in bad:
            with self.subTest(label):
                rc, out = self.build("--prepare-only", "--deps-receipt", self.receipt(**override))
                self.assertEqual(rc, 4, f"{label}\n{out}")
                self.assertEqual(self.count("npm ci"), 0, f"{label}: no silent reinstall")
                self.assertFalse((self.state / "deps.ok").exists(), label)
        rc, out = self.build("--prepare-only", "--deps-receipt", self.receipt())
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.count("npm ci"), 0, "a valid receipt skips npm ci")
        self.assertIn("written_by=adopt receipt", (self.state / "deps.ok").read_text())


if __name__ == "__main__":
    import unittest
    unittest.main()
