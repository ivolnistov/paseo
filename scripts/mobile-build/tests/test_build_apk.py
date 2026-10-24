"""build-apk.sh preflight, dry-run, locking and cache contracts with fake tools.

Contract (taskcards S2 plus the test seams fixed here):
  scripts/build-apk.sh --src DIR --expected-sha SHA --version V --variant production --abi arm64-v8a
    --google-services FILE --keystore DIR --expected-cert SHA256 --out DIR --state-dir DIR
    --java-home DIR --android-home DIR [--with-jvm-tests] [--dry-run]
  --keystore DIR holds release.p12 and storepass (mode 0600/0400); the password is passed only as file:.
  Exit codes: 0 ok, 2 usage, 3 precondition, 4 verification, 5 refused overwrite.
  Preflight failures exit 3 before any tool runs and write nothing under --out or --state-dir.
  versionName/code come from <src>/packages/app/native-release-version.js.
  The build lock is <state-dir>/build.lock (lock.sh); deps use <state-dir>/deps.ok (deps-state.sh).
  Expo prebuild never gets --clean and the existing packages/app/android is never deleted.
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import BUILD_APK, EXPECTED_CERT, STOREPASS, SourceFixture, git, write  # noqa: E402

TOOL_PREFIXES = ("npm ci", "npx", "gradlew", "apksigner", "zipalign", "aapt2", "dexdump", "adb")


class BuildApkTest(support.ToolTestCase):
    required = (BUILD_APK, support.DEPS_STATE, support.LOCK, support.VERIFY_APK)

    def setUp(self):
        super().setUp()
        self.fx = SourceFixture(self.root)
        # --with-jvm-tests runs the keyboard module's JVM tests, so the source must declare that Android module.
        write(self.fx.src / "packages/app/modules/paseo-hardware-keyboard/android/build.gradle", "// module\n")
        git(self.fx.src, "add", "-A")
        git(self.fx.src, "commit", "-qm", "keyboard module")
        r = self.root
        self.out = r / "out"
        self.state = r / "state"
        self.jdk = r / "jdk"
        write(self.jdk / "bin/java", support.FAKE_JAVA, 0o755)
        self.sdk = r / "sdk"
        for tool in ("zipalign", "apksigner", "aapt2", "dexdump"):
            write(self.sdk / "build-tools/36.0.0" / tool, support.FAIL_TOOL, 0o755)
        write(self.sdk / "ndk/27.1.12297006/source.properties", "Pkg.Revision = 27.1.12297006\n")
        write(self.sdk / "platforms/android-36/android.jar", "jar\n")
        self.keys = r / "keys"
        write(self.keys / "release.p12", "not a real keystore\n", 0o600)
        write(self.keys / "storepass", STOREPASS + "\n", 0o600)
        self.gs = write(r / "inputs/google-services.json",
                        '{"project_info": {}, "client": [{"client_info": {"mobilesdk_app_id": "1:2:android:3", '
                        '"android_client_info": {"package_name": "sh.paseo"}}}]}\n', 0o600)

    def build_args(self, *extra, expected_sha=None):
        return ["bash", BUILD_APK, "--src", self.fx.src, "--expected-sha", expected_sha or self.fx.head,
                "--version", "0.13.0-beta.2", "--variant", "production", "--abi", "arm64-v8a",
                "--google-services", self.gs, "--keystore", self.keys, "--expected-cert", EXPECTED_CERT,
                "--out", self.out, "--state-dir", self.state, "--java-home", self.jdk, "--android-home", self.sdk,
                *extra]

    def build(self, *extra, expected_sha=None, **env):
        return self.run_cmd(self.build_args(*extra, expected_sha=expected_sha), timeout=300, **env)

    def tool_calls(self):
        return [c for c in self.calls.read_text().splitlines() if c.startswith(TOOL_PREFIXES)]

    def assert_refused_before_work(self, rc, out, code=3):
        self.assertEqual(rc, code, out)
        self.assertEqual(self.tool_calls(), [], out)
        self.assertFalse(self.out.exists(), "nothing written under --out")
        self.assertFalse(self.state.exists() and any(self.state.iterdir()), "nothing written under --state-dir")

    def test_help(self):
        rc, out = self.run_cmd(["bash", BUILD_APK, "--help"])
        self.assertEqual(rc, 0, out)
        self.assertIn("--expected-sha", out)

    def test_missing_arguments_are_usage_errors(self):
        rc, out = self.run_cmd(["bash", BUILD_APK, "--src", self.fx.src])
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.tool_calls(), [])

    def test_bad_provenance_is_a_usage_error_before_any_work(self):
        digest = "ab" * 32
        cases = [
            ("missing =", ["--provenance", "client.patch"]),
            ("bad name", ["--provenance", f"../escape={digest}"]),
            ("empty name", ["--provenance", f"={digest}"]),
            ("short digest", ["--provenance", "client.patch=" + "ab" * 16]),
            ("non-hex digest", ["--provenance", "client.patch=" + "zz" * 32]),
            ("conflicting duplicate", ["--provenance", f"client.patch={digest}", "--provenance", "client.patch=" + "cd" * 32]),
        ]
        for label, flags in cases:
            with self.subTest(label):
                self.assert_refused_before_work(*self.build("--dry-run", *flags), code=2)

    def test_provenance_duplicates_and_case_are_normalized(self):
        digest = "ab" * 32
        rc, out = self.build("--dry-run", "--provenance", f"client.patch={digest.upper()}",
                             "--provenance", f"client.patch={digest}", "--provenance", "beta1-port.patch=" + "cd" * 32)
        self.assert_refused_before_work(rc, out, code=0)
        self.assertEqual(out.count(f"client.patch={digest}"), 1, out)
        self.assertNotIn(digest.upper(), out)
        self.assertIn("beta1-port.patch=" + "cd" * 32, out)

    def test_version_must_equal_source_package_version(self):
        # The fixture source is 0.13.0-beta.2: another beta, the bare release or a suffix variant is refused.
        for version in ("0.13.0-beta.1", "0.13.0", "0.13.0-beta.20"):
            with self.subTest(version):
                args = [a if a != "0.13.0-beta.2" else version for a in self.build_args("--dry-run")]
                self.assert_refused_before_work(*self.run_cmd(args, timeout=300))

    def test_unsupported_variant_refused_before_work(self):
        # app.config.js declares production and development; any other name silently builds production config.
        # Give the package a google-services client, so only the variant itself can be the reason to refuse.
        self.gs.write_text('{"project_info": {}, "client": [{"client_info": {"mobilesdk_app_id": "1:2:android:3", '
                           '"android_client_info": {"package_name": "sh.paseo.huawei"}}}]}\n')
        args = self.build_args("--dry-run")
        args[args.index("production")] = "huawei"
        rc, out = self.run_cmd(args + ["--package", "sh.paseo.huawei"], timeout=300)
        self.assertIn(rc, (2, 3), out)
        self.assertEqual(self.tool_calls(), [], out)
        self.assertFalse(self.out.exists())

    def test_gnu_stat_fallback_reads_only_the_successful_mode(self):
        # GNU stat treats `-f %Lp` as filesystem mode: it prints a block for the file and exits 1.
        write(self.bin / "stat", """#!/bin/sh
if [ "$1" = "-f" ]; then echo "  File: \\"$3\\""; echo "    ID: 0 Namelen: 255 Type: apfs"; exit 1; fi
if [ "$1" = "-c" ] && [ "$2" = "%a" ]; then python3 -I -c 'import os,sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "$3"; exit 0; fi
exit 2
""", 0o755)
        rc, out = self.build("--dry-run")
        self.assert_refused_before_work(rc, out, code=0)

    def test_unignored_out_or_state_inside_source_refused(self):
        for flag, rel in (("--out", "artifacts-out"), ("--state-dir", "build-state")):
            with self.subTest(flag):
                args = self.build_args("--dry-run")
                args[args.index(flag) + 1] = self.fx.src / rel
                rc, out = self.run_cmd(args, timeout=300)
                self.assertEqual(rc, 3, out)
                self.assertEqual(self.tool_calls(), [], out)
                self.assertFalse((self.fx.src / rel).exists())
                self.assertEqual(self.fx.status(), "")
        # Inside an ignored directory (.dev/) both stay allowed.
        args = self.build_args("--dry-run")
        args[args.index("--out") + 1] = self.fx.src / ".dev/out"
        args[args.index("--state-dir") + 1] = self.fx.src / ".dev/state"
        rc, out = self.run_cmd(args, timeout=300)
        self.assertEqual(rc, 0, out)

    def test_jvm_tests_without_keyboard_module_refused(self):
        git(self.fx.src, "rm", "-rq", "packages/app/modules/paseo-hardware-keyboard")
        git(self.fx.src, "commit", "-qm", "no keyboard module")
        self.assert_refused_before_work(*self.build("--dry-run", "--with-jvm-tests"))

    def test_dry_run_derives_version_and_writes_nothing(self):
        tracked = lambda: {k: v for k, v in self.snapshot(self.fx.src).items() if k != ".git" and not k.startswith(".git/")}  # noqa: E731
        before = tracked()
        rc, out = self.build("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("424242", out, "versionCode must come from native-release-version.js")
        self.assertIn("7.8.9", out, "versionName must come from native-release-version.js")
        self.assertNotIn("13000", out, "no hardcoded versionCode")
        self.assertNotIn("--clean", out)
        self.assert_refused_before_work(rc, out, code=0)
        self.assertEqual(tracked(), before)

    def test_wrong_head_refused(self):
        self.assert_refused_before_work(*self.build(expected_sha="0" * 40))

    def test_dirty_tree_refused(self):
        write(self.fx.src / "stray.txt", "dirty\n")
        self.assert_refused_before_work(*self.build())

    def test_modified_tracked_file_refused(self):
        write(self.fx.src / "packages/app/app.config.js", "module.exports = () => ({ name: 'X' });\n")
        self.assert_refused_before_work(*self.build())

    def test_missing_jdk_refused(self):
        (self.jdk / "bin/java").unlink()
        self.assert_refused_before_work(*self.build())

    def test_world_readable_password_file_refused(self):
        (self.keys / "storepass").chmod(0o644)
        self.assert_refused_before_work(*self.build())

    def test_world_readable_google_services_refused(self):
        self.gs.chmod(0o644)
        self.assert_refused_before_work(*self.build())

    def test_symlinked_node_modules_refused(self):
        nm = self.fx.src / "node_modules"
        elsewhere = self.root / "shared-node-modules"
        nm.rename(elsewhere)
        nm.symlink_to(elsewhere)
        self.assert_refused_before_work(*self.build())

    def test_held_lock_refused_and_kept(self):
        holder = support.live_process()
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        write(self.state / "build.lock/pid", f"{holder.pid}\n")
        rc, out = self.build()
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.tool_calls(), [], out)
        self.assertEqual((self.state / "build.lock/pid").read_text().strip(), str(holder.pid))
        self.assertFalse(self.out.exists())

    def test_stale_lock_is_reported_as_stale_and_kept(self):
        # A killed build (SIGKILL skips the EXIT trap) leaves a lock whose owner is dead.
        source_lock = pathlib.Path(support.git(self.fx.src, "rev-parse", "--absolute-git-dir")) / "paseo-mobile-build.lock"
        for label, lock in (("state lock", self.state / "build.lock"), ("source lock", source_lock)):
            with self.subTest(label):
                dead = support.dead_pid()
                write(lock / "pid", f"{dead}\n")
                rc, out = self.build()
                self.assertEqual(rc, 3, out)
                self.assertIn("stale", out.lower(), f"{label}: a dead owner is reported as stale, not as a running build")
                self.assertIn(str(dead), out)
                self.assertEqual((lock / "pid").read_text().strip(), str(dead), "the stale lock is kept for the operator")
                self.assertEqual(self.tool_calls(), [], out)
                shutil.rmtree(lock)

    def test_fdroid_switch_is_refused_before_work(self):
        # The tool builds signed local release APKs only; the F-Droid source build changes the versionCode scheme.
        self.assert_refused_before_work(*self.build(PASEO_FDROID_BUILD="1"))

    def test_failed_build_keeps_project_reuses_deps_and_never_cleans(self):
        sentinel = write(self.fx.src / "packages/app/android/app/.cxx/keep.txt", "warm native intermediates\n")
        rc, out = self.build("--with-jvm-tests")
        self.assertNotEqual(rc, 0, out)
        npm_ci = len(self.calls_matching("npm ci"))
        self.assertEqual(npm_ci, 1, "cold state installs once")
        prebuilds = self.calls_matching("npx")
        self.assertTrue(prebuilds and all("prebuild" in c and "--clean" not in c for c in prebuilds), prebuilds)
        gradle = self.calls_matching("gradlew")
        self.assertEqual(len(gradle), 1, gradle)
        for flag in ("-PreactNativeArchitectures=arm64-v8a", "--max-workers=1", "--no-daemon", ":app:assembleRelease",
                     ":paseo-hardware-keyboard:testReleaseUnitTest"):
            self.assertIn(flag, gradle[0])
        self.assertNotIn(" clean", gradle[0])
        self.assertNotIn("--rerun-tasks", gradle[0])
        self.assertTrue(sentinel.is_file(), "android/ and .cxx intermediates survive")
        self.assertEqual(self.fx.status(), "", "source tree stays clean")
        self.assertFalse(list(self.out.glob("*.apk")), "a failed build delivers nothing")
        self.assertFalse((self.state / "build.lock").exists(), "lock released after failure")

        rc, out = self.build("--with-jvm-tests")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(len(self.calls_matching("npm ci")), npm_ci, "unchanged fingerprint reuses node_modules")
        self.assertTrue(sentinel.is_file())

    def test_secrets_never_reach_arguments_or_output(self):
        rc, out = self.build("--with-jvm-tests")
        self.assertNotIn(STOREPASS, out)
        self.assertNotIn(STOREPASS, self.calls.read_text(), "password only via file:, never as an argument")
        self.assertNotIn("1:2:android:3", out, "google-services values are never printed")

    def test_wrong_signing_cert_refused_before_gradle(self):
        rc, out = self.build(STUB_CERT="f" * 64)
        self.assertIn(rc, (3, 4), out)
        self.assertEqual(self.calls_matching("gradlew"), [], out)
        self.assertFalse(list(self.out.glob("*.apk")) if self.out.exists() else [])


if __name__ == "__main__":
    import unittest
    unittest.main()
