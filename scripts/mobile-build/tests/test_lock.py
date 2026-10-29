"""lock.sh: one exclusive build lock per worktree."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import write  # noqa: E402


class LockTest(support.ToolTestCase):
    """lock.sh acquire|release --lock-dir DIR --pid PID: atomic mkdir lock holding a pid file.

    acquire: exit 0 and DIR/pid == PID; a lock held by a live pid is refused with exit 3 and left unchanged; a lock
    whose pid is dead is reported as stale (pid in the output) and refused (exit 3); it is never taken over.
    release: removes the lock only for its owner pid; another owner's lock is kept and release exits non-zero.
    """

    required = (support.LOCK,)

    def setUp(self):
        super().setUp()
        self.lock = self.root / "state/build.lock"
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def owner(self):
        p = support.live_process()
        self.procs.append(p)
        return p.pid

    def lock_cmd(self, action, pid):
        return self.run_cmd(["bash", support.LOCK, action, "--lock-dir", self.lock, "--pid", pid])

    def test_acquire_records_owner(self):
        pid = self.owner()
        rc, out = self.lock_cmd("acquire", pid)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.lock / "pid").read_text().strip(), str(pid))

    def test_live_lock_refused_and_kept(self):
        first, second = self.owner(), self.owner()
        self.assertEqual(self.lock_cmd("acquire", first)[0], 0)
        rc, out = self.lock_cmd("acquire", second)
        self.assertEqual(rc, 3, out)
        self.assertIn(str(first), out, "the refusal names the holder")
        self.assertEqual((self.lock / "pid").read_text().strip(), str(first))

    def test_concurrent_acquires_have_one_winner(self):
        pids = [self.owner() for _ in range(8)]
        procs = [subprocess.Popen(["bash", str(support.LOCK), "acquire", "--lock-dir", str(self.lock), "--pid", str(p)],
                                  env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in pids]
        codes = [p.wait(timeout=30) for p in procs]
        self.assertEqual(codes.count(0), 1, codes)
        self.assertIn(int((self.lock / "pid").read_text().strip()), pids)

    def test_stale_lock_is_reported(self):
        dead = support.dead_pid()
        write(self.lock / "pid", f"{dead}\n")
        new = self.owner()
        rc, out = self.lock_cmd("acquire", new)
        self.assertIn("stale", out.lower(), out)
        self.assertIn(str(dead), out)
        # Refused, never taken over: a dead holder is reported and left for the operator to clear.
        self.assertEqual(rc, 3, out)
        self.assertEqual((self.lock / "pid").read_text().strip(), str(dead))

    def test_release_only_by_owner(self):
        holder, other = self.owner(), self.owner()
        self.assertEqual(self.lock_cmd("acquire", holder)[0], 0)
        rc, out = self.lock_cmd("release", other)
        self.assertNotEqual(rc, 0, out)
        self.assertTrue((self.lock / "pid").exists())
        rc, out = self.lock_cmd("release", holder)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
