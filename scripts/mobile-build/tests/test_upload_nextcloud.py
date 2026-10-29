"""upload-nextcloud.sh against a temp root only. A real Nextcloud folder is never used.

Contract: scripts/upload-nextcloud.sh --apk FILE --manifest FILE --root DIR [--subdir REL] [--dry-run]
  Manifest (JSON written by build-apk.sh): artifact.file == APK basename, artifact.sha256 == sha256(APK),
  source.sha (40 hex), version, variant. A mismatch exits 4 and writes nothing.
  Destination <root>/<subdir>/ receives three artifact-specific files:
    <apk>, <apk>.build-manifest.json (byte copy of the manifest), <apk>.sha256 ("<hex>  <apk>").
  An identical existing file is a no-op; a differing existing file exits 5 and is preserved. Paths that leave the
  root (.., absolute, symlink escape) are refused. Copies go to a temp name, then rename; a failure leaves no final
  or partial file. --dry-run exits 0 and writes nothing. The output says a local copy does not prove remote sync.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import UPLOAD, make_apk, sha256, write  # noqa: E402

SUBDIR = "Paseo/mobile"


class UploadTest(support.ToolTestCase):
    required = (UPLOAD,)

    def setUp(self):
        super().setUp()
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.beta2, self.manifest2 = self.artifact("paseo-v0.13.0-beta.2-sh.paseo-arm64-v8a.apk", "0.13.0-beta.2", "b2")
        self.beta1, self.manifest1 = self.artifact("paseo-v0.13.0-beta.1-sh.paseo-arm64-v8a.apk", "0.13.0-beta.1", "b1")

    def artifact(self, name, version, salt):
        apk = make_apk(self.root / "dist" / version / name, ["libhermesvm.so", "libreactnative.so"],
                       extra={"assets/salt.txt": salt})
        return apk, self.manifest(apk, version, "build-manifest.json")

    def manifest(self, apk, version, filename, **override):
        data = {
            "schema": "paseo-mobile-build-manifest-v1",
            "artifact": {"file": apk.name, "sha256": sha256(apk), "size": apk.stat().st_size},
            "source": {"sha": "30d8cb044702fa873057ecc5c2676c7964a3da33", "tree": "a23cebb0" + "0" * 32},
            "version": version, "versionName": "0.13.0", "versionCode": 13000,
            "variant": "production", "package": "sh.paseo", "abi": "arm64-v8a",
        }
        data.update(override)
        return write(apk.parent / filename, json.dumps(data, indent=2) + "\n")

    def upload(self, apk, manifest, *extra, subdir=SUBDIR):
        return self.run_cmd(["bash", UPLOAD, "--apk", apk, "--manifest", manifest, "--root", self.cloud,
                             "--subdir", subdir, *extra])

    def dest(self, apk):
        d = self.cloud / SUBDIR
        return d / apk.name, d / f"{apk.name}.build-manifest.json", d / f"{apk.name}.sha256"

    def assert_nothing_written(self, before_cloud, before_outside):
        self.assertEqual(self.snapshot(self.cloud), before_cloud)
        self.assertEqual(self.snapshot(self.outside), before_outside)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, self.manifest2, "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assert_nothing_written(*before)
        self.assertFalse((self.cloud / "Paseo").exists())

    def test_upload_writes_artifact_specific_files(self):
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 0, out)
        apk, manifest, sidecar = self.dest(self.beta2)
        self.assertEqual(apk.read_bytes(), self.beta2.read_bytes())
        self.assertEqual(manifest.read_bytes(), self.manifest2.read_bytes())
        self.assertEqual(sidecar.read_text().split(), [sha256(self.beta2), self.beta2.name])
        self.assertEqual(sorted(p.name for p in apk.parent.iterdir()), sorted(p.name for p in (apk, manifest, sidecar)),
                         "no temp or partial files remain")
        self.assertIn("sync", out.lower(), "output must say a local copy is not proof of remote sync")

    def test_two_artifacts_do_not_share_manifest_or_checksum(self):
        self.assertEqual(self.upload(self.beta1, self.manifest1)[0], 0)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 0, out)
        for src, man in ((self.beta1, self.manifest1), (self.beta2, self.manifest2)):
            apk, manifest, sidecar = self.dest(src)
            self.assertEqual(apk.read_bytes(), src.read_bytes())
            self.assertEqual(manifest.read_bytes(), man.read_bytes())
            self.assertEqual(sidecar.read_text().split()[0], sha256(src))
        self.assertFalse((self.cloud / SUBDIR / "build-manifest.json").exists(), "no global manifest name")

    def test_identical_rerun_is_a_no_op(self):
        self.assertEqual(self.upload(self.beta2, self.manifest2)[0], 0)
        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in self.dest(self.beta2)}
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 0, out)
        self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in self.dest(self.beta2)}, before)

    def test_differing_existing_apk_refused_and_preserved(self):
        apk, manifest, sidecar = self.dest(self.beta2)
        write(apk, "someone else's bytes\n")
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 5, out)
        self.assertEqual(apk.read_text(), "someone else's bytes\n")
        self.assertFalse(manifest.exists() or sidecar.exists(), "a refused upload writes none of the set")

    def test_differing_existing_manifest_refused(self):
        _, manifest, _ = self.dest(self.beta2)
        write(manifest, "{}\n")
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 5, out)
        self.assertEqual(manifest.read_text(), "{}\n")
        apk, _, sidecar = self.dest(self.beta2)
        self.assertFalse(apk.exists() or sidecar.exists(), "a refused upload writes none of the set")

    def test_apk_sha_mismatch_refused(self):
        bad = self.manifest(self.beta2, "0.13.0-beta.2", "bad.json",
                            artifact={"file": self.beta2.name, "sha256": "0" * 64, "size": 1})
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, bad)
        self.assertEqual(rc, 4, out)
        self.assert_nothing_written(*before)

    def test_manifest_for_another_file_refused(self):
        other = self.manifest(self.beta2, "0.13.0-beta.2", "other.json",
                              artifact={"file": "other.apk", "sha256": sha256(self.beta2), "size": 1})
        rc, out = self.upload(self.beta2, other)
        self.assertEqual(rc, 4, out)
        self.assertFalse((self.cloud / "Paseo").exists())

    def test_manifest_without_source_sha_refused(self):
        bad = self.manifest(self.beta2, "0.13.0-beta.2", "nosource.json", source={})
        rc, out = self.upload(self.beta2, bad)
        self.assertEqual(rc, 4, out)
        self.assertFalse((self.cloud / "Paseo").exists())

    def test_parent_traversal_refused(self):
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, self.manifest2, subdir="../outside")
        self.assertIn(rc, (2, 3), out)
        self.assert_nothing_written(*before)

    def test_absolute_subdir_refused(self):
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, self.manifest2, subdir=str(self.outside))
        self.assertIn(rc, (2, 3), out)
        self.assert_nothing_written(*before)

    def test_symlink_escape_refused(self):
        (self.cloud / "Paseo").symlink_to(self.outside, target_is_directory=True)
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertIn(rc, (2, 3), out)
        self.assert_nothing_written(*before)

    def test_missing_root_refused(self):
        rc, out = self.run_cmd(["bash", UPLOAD, "--apk", self.beta2, "--manifest", self.manifest2,
                                "--root", self.root / "no-such-root", "--subdir", SUBDIR])
        self.assertEqual(rc, 3, out)
        self.assertFalse((self.root / "no-such-root").exists())

    def test_failed_copy_leaves_no_final_or_partial_file(self):
        dest_dir = self.cloud / SUBDIR
        dest_dir.mkdir(parents=True)
        dest_dir.chmod(0o555)
        self.addCleanup(dest_dir.chmod, 0o755)
        if os.access(dest_dir, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(list(dest_dir.iterdir()), [])

    # OS-level failure boundaries. Faults reach the uploader only through its injected destination
    # (main(argv, destination=...), open_destination, DestDir, write_all(todo, dest)); no os function is patched.
    def module(self):
        spec = importlib.util.spec_from_file_location("upload_nextcloud_probe", support.HELPERS / "upload_nextcloud.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def main(self, mod, apk, manifest, destination):
        args = ["--apk", str(apk), "--manifest", str(manifest), "--root", str(self.cloud), "--subdir", SUBDIR]
        return mod.main(args, destination=destination)

    def test_unwritable_root_maps_to_3(self):
        self.cloud.chmod(0o555)
        self.addCleanup(self.cloud.chmod, 0o755)
        if os.access(self.cloud, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 3, out)
        self.assertNotIn("Traceback", out)

    def test_existing_directory_component_is_reused(self):
        # End state of a concurrent mkdir: the component already is a real directory.
        mod = self.module()
        (self.cloud / SUBDIR).mkdir(parents=True)
        dest = mod.open_destination(self.cloud, pathlib.PurePosixPath(SUBDIR), True)
        dest.close()
        self.assertTrue((self.cloud / SUBDIR).is_dir() and not (self.cloud / "Paseo").is_symlink())

    def test_symlink_component_is_refused(self):
        # End state of a hostile concurrent writer: a component is a symlink out of the root.
        mod = self.module()
        (self.cloud / "Paseo").symlink_to(self.outside, target_is_directory=True)
        before = self.snapshot(self.outside)
        with self.assertRaises(mod.Stop) as refused:
            mod.open_destination(self.cloud, pathlib.PurePosixPath(SUBDIR), True)
        self.assertEqual(refused.exception.code, 3)
        self.assertEqual(self.snapshot(self.outside), before)

    def test_interrupt_before_second_link_leaves_no_file(self):
        mod = self.module()
        dest_dir = self.cloud / SUBDIR

        def destination(root, rel, create):
            return InterruptAt(mod.open_destination(root, rel, create), before_link=2)

        with self.assertRaises(KeyboardInterrupt):
            self.main(mod, self.beta2, self.manifest2, destination)
        self.assertEqual(sorted(p.name for p in dest_dir.iterdir()), [], "temp and placed files are removed")

    def test_interrupt_right_after_link_leaves_only_foreign_files(self):
        mod = self.module()
        dest_dir = self.cloud / SUBDIR
        dest_dir.mkdir(parents=True)
        write(dest_dir / "foreign.txt", "someone else's file\n")

        def destination(root, rel, create):
            return InterruptAt(mod.open_destination(root, rel, create), after_link=1)

        with self.assertRaises(KeyboardInterrupt):
            self.main(mod, self.beta2, self.manifest2, destination)
        self.assertEqual(sorted(p.name for p in dest_dir.iterdir()), ["foreign.txt"])
        self.assertEqual((dest_dir / "foreign.txt").read_text(), "someone else's file\n")

    def test_component_swapped_after_open_cannot_redirect_writes(self):
        mod = self.module()
        before = self.snapshot(self.outside)
        opened = self.cloud / "Paseo/mobile-opened"

        def destination(root, rel, create):
            dest = mod.open_destination(root, rel, create)
            (self.cloud / SUBDIR).rename(opened)  # the opened directory moves away...
            (self.cloud / SUBDIR).symlink_to(self.outside, target_is_directory=True)  # ...and a symlink takes its name
            return dest

        rc = self.main(mod, self.beta2, self.manifest2, destination)
        self.assertEqual(rc, 0)
        self.assertEqual(self.snapshot(self.outside), before, "nothing written through the swapped component")
        self.assertEqual((opened / self.beta2.name).read_bytes(), self.beta2.read_bytes())

    def test_second_uploader_is_excluded_until_the_first_releases(self):
        # Another uploader holds the root directory lock (flock on the root fd, as the uploader takes it).
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c",
             "import fcntl,os,sys; fd=os.open(sys.argv[1], os.O_RDONLY|os.O_DIRECTORY); "
             "fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('held', flush=True); sys.stdin.read()",
             str(self.cloud)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 3, out)
        self.assert_nothing_written(*before)
        holder.stdin.close()
        holder.wait(timeout=10)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 0, out)

    def test_manifest_with_non_object_parts_is_refused(self):
        good = json.loads(self.manifest2.read_text())
        cases = {"document is a list": [good],
                 "artifact is a string": {**good, "artifact": "paseo.apk"},
                 "source is a string": {**good, "source": "30d8cb04"}}
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        for label, data in cases.items():
            with self.subTest(label):
                bad = write(self.beta2.parent / "shape.json", json.dumps(data))
                rc, out = self.upload(self.beta2, bad)
                self.assertEqual(rc, 4, out)
                self.assertNotIn("Traceback", out)
                self.assert_nothing_written(*before)

    def test_unreadable_inputs_map_to_3(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        self.beta2.chmod(0o000)
        self.addCleanup(self.beta2.chmod, 0o644)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 3, out)
        self.assertNotIn("Traceback", out)
        self.beta2.chmod(0o644)

        apk, _, _ = self.dest(self.beta2)
        write(apk, "an existing unreadable destination file\n")
        apk.chmod(0o000)
        self.addCleanup(apk.chmod, 0o644)
        rc, out = self.upload(self.beta2, self.manifest2)
        self.assertEqual(rc, 3, out)
        self.assertNotIn("Traceback", out)
        apk.chmod(0o644)
        self.assertEqual(apk.read_text(), "an existing unreadable destination file\n")

    def test_non_apk_artifact_refused(self):
        # The upload capability is APK-only; daemon or other archives are excluded even with a consistent manifest.
        tgz = write(self.root / "dist/daemon/paseo-daemon-v0.13.0.tgz", "daemon archive bytes\n")
        manifest = self.manifest(tgz, "0.13.0", "daemon-manifest.json")
        before = self.snapshot(self.cloud), self.snapshot(self.outside)
        for extra in (("--dry-run",), ()):
            with self.subTest(extra or "real run"):
                rc, out = self.upload(tgz, manifest, *extra)
                self.assertEqual(rc, 4, out)
                self.assert_nothing_written(*before)


class InterruptAt:
    """A DestDir stand-in that delegates to the real one and raises KeyboardInterrupt at one link call."""

    def __init__(self, inner, before_link=None, after_link=None):
        self.inner, self.before, self.after, self.links = inner, before_link, after_link, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def link(self, temp, name):
        self.links += 1
        if self.links == self.before:
            raise KeyboardInterrupt
        self.inner.link(temp, name)
        if self.links == self.after:
            raise KeyboardInterrupt

if __name__ == "__main__":
    import unittest
    unittest.main()
