"""deps-state.sh: npm ci runs only when the dependency fingerprint is missing or invalid, and a success marker is
written only after a substantiated install.

Contract: deps-state.sh check|install --src DIR --state-dir DIR
  check:   exit 0 cache hit, 1 miss (reinstall needed), 3 precondition (e.g. symlinked node_modules). Never runs npm ci.
  install: on hit runs no npm ci and exits 0; on miss removes any old <state-dir>/deps.ok first, runs `npm ci` in
           --src, and writes <state-dir>/deps.ok only if npm ci exits 0 and node_modules/.package-lock.json exists,
           package-lock.json is unchanged and node_modules is a real directory. Any failure exits non-zero.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import DEPS_STATE, SourceFixture, write  # noqa: E402


class DepsStateTest(support.ToolTestCase):
    required = (DEPS_STATE,)

    def setUp(self):
        super().setUp()
        self.fx = SourceFixture(self.root)
        self.state = self.root / "state"
        self.marker = self.state / "deps.ok"

    def deps(self, action, **env):
        return self.run_cmd(["bash", DEPS_STATE, action, "--src", self.fx.src, "--state-dir", self.state], **env)

    def npm_ci_count(self):
        return len(self.calls_matching("npm ci"))

    def install_ok(self):
        rc, out = self.deps("install")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.marker.is_file(), out)
        return out

    def test_cold_state_is_a_miss_and_install_writes_marker(self):
        rc, out = self.deps("check")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.npm_ci_count(), 0, "check must never install")
        self.install_ok()
        self.assertEqual(self.npm_ci_count(), 1)
        self.assertTrue(all(f"@{self.fx.src}" in c for c in self.calls_matching("npm ci")), "npm ci runs in --src")
        self.assertEqual(self.deps("check")[0], 0)

    def test_cache_hit_skips_npm_ci(self):
        self.install_ok()
        marker = self.marker.read_bytes()
        rc, out = self.deps("install")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.npm_ci_count(), 1, "a valid fingerprint must not reinstall")
        self.assertEqual(self.marker.read_bytes(), marker)

    def assert_invalidated(self, mutate=lambda: None, **env):
        self.install_ok()
        mutate()
        rc, out = self.deps("check", **env)
        self.assertEqual(rc, 1, out)
        rc, out = self.deps("install", **env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.npm_ci_count(), 2, out)
        self.assertEqual(self.deps("check", **env)[0], 0)

    def test_lockfile_change_invalidates(self):
        self.assert_invalidated(lambda: write(self.fx.src / "package-lock.json", '{"lockfileVersion": 3, "fixture": 2}\n'))

    def test_node_version_change_invalidates(self):
        self.assert_invalidated(STUB_NODE_VERSION="v24.0.0")

    def test_npm_version_change_invalidates(self):
        self.assert_invalidated(STUB_NPM_VERSION="10.9.0")

    def test_patches_change_invalidates(self):
        self.assert_invalidated(lambda: write(self.fx.src / "patches/example+1.0.0.patch", "--- a\n+++ b\n+x\n"))

    def test_added_patch_invalidates(self):
        self.assert_invalidated(lambda: write(self.fx.src / "patches/new+2.0.0.patch", "--- c\n"))

    def test_postinstall_script_change_invalidates(self):
        self.assert_invalidated(lambda: write(self.fx.src / "scripts/postinstall-patches.mjs", "// changed\n"))

    def assert_failed_install_leaves_no_marker(self, mode):
        self.install_ok()
        write(self.fx.src / "package-lock.json", '{"lockfileVersion": 3, "fixture": 3}\n')
        rc, out = self.deps("install", STUB_NPM_CI=mode)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.marker.exists(), f"stale or new deps.ok after {mode}: {out}")
        self.assertEqual(self.deps("check")[0], 1, "the next run must reinstall")
        rc, out = self.deps("install")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.marker.is_file())

    def test_failed_npm_ci_leaves_no_marker(self):
        self.assert_failed_install_leaves_no_marker("fail")

    def test_killed_npm_ci_leaves_no_marker(self):
        self.assert_failed_install_leaves_no_marker("kill")

    def test_exit_zero_without_installed_lock_is_not_success(self):
        self.assert_failed_install_leaves_no_marker("nolock")

    def test_install_that_rewrites_the_lockfile_is_not_success(self):
        rc, out = self.deps("install", STUB_NPM_CI="mutate")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(self.marker.exists(), out)

    def test_marker_alone_does_not_bless_a_damaged_install(self):
        self.install_ok()
        (self.fx.src / "node_modules/.package-lock.json").unlink()
        self.assertNotEqual(self.deps("check")[0], 0, "a marker without the installed lock is not a hit")

    def receipt(self, **override):
        fp = self.run_cmd(["bash", DEPS_STATE, "fingerprint", "--src", self.fx.src, "--state-dir", self.state])[1]
        cur = dict(line.split("=", 1) for line in fp.splitlines() if "=" in line)
        data = {"status": "success", "exit": 0, "command": "npm ci --prefer-offline", "worktree": str(self.fx.src),
                "lockSha256Before": cur["lock"], "lockSha256After": cur["lock"],
                "installedLockSha256": cur["installed_lock"],
                "workspacePackageJsonsSha256": cur["workspace_package_jsons"], "patchesDirSha256": cur["patches"],
                "postinstallScriptSha256": cur["postinstall"], "node": cur["node"], "npm": cur["npm"],
                "platform": cur["platform"], "arch": cur["arch"],
                "postconditions": {"nodeModulesRealDir": True, "hiddenLockPresent": True, "lockUnchanged": True}}
        data.update(override)
        return write(self.root / f"receipt-{len(override)}.json", json.dumps(data))

    def test_explicit_receipt_is_validated_even_on_a_warm_hit(self):
        self.install_ok()
        marker = (self.marker.stat().st_ino, self.marker.read_bytes())
        rc, out = self.run_cmd(["bash", DEPS_STATE, "adopt", "--src", self.fx.src, "--state-dir", self.state,
                                "--receipt", self.receipt(installedLockSha256="0" * 64)])
        self.assertEqual(rc, 4, "a mismatching receipt is refused although the install is warm\n" + out)
        self.assertEqual((self.marker.stat().st_ino, self.marker.read_bytes()), marker, "marker untouched")
        rc, out = self.run_cmd(["bash", DEPS_STATE, "adopt", "--src", self.fx.src, "--state-dir", self.state,
                                "--receipt", self.receipt()])
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.marker.stat().st_ino, self.marker.read_bytes()), marker, "a valid warm receipt keeps the marker")

    def test_symlinked_node_modules_rejected(self):
        self.install_ok()
        nm = self.fx.src / "node_modules"
        elsewhere = self.root / "shared-node-modules"
        nm.rename(elsewhere)
        nm.symlink_to(elsewhere)
        rc, out = self.deps("check")
        self.assertEqual(rc, 3, out)
        rc, out = self.deps("install")
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.npm_ci_count(), 1, "no install through a symlinked node_modules")
        self.assertTrue(nm.is_symlink() and elsewhere.is_dir(), "the symlink target is left alone")


if __name__ == "__main__":
    import unittest
    unittest.main()
