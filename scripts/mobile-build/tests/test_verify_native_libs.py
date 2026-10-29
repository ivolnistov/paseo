"""verify_apk.py native-libs: the required JS engine library comes from the installed React Native source.

Contract: python3 -I verify_apk.py native-libs --apk FILE --rn-root DIR --abi ABI
  Reads <rn-root>/ReactAndroid/src/main/java/com/facebook/hermes/reactexecutor/HermesExecutor.kt and finds the
  SoLoader.loadLibrary("<name>") calls. Exactly one supported standalone engine name (hermes or hermesvm) must be
  loaded; the APK must then contain lib/<abi>/lib<engine>.so and lib/<abi>/libreactnative.so. Other loader names
  (hermes_executor) are merged by RN and are not required as separate .so files. Every lib/ entry must be <abi>.
  Exit 0 pass, 4 verification failure; a missing/unparseable/ambiguous/unknown engine fails closed (non-zero).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import support  # noqa: E402
from support import (COMMON_LIBS, HERMES_EXECUTOR_LEGACY, HERMES_EXECUTOR_PATH, HERMES_EXECUTOR_RN086,  # noqa: E402
                     make_apk, write)


class NativeLibsTest(support.ToolTestCase):
    required = (support.VERIFY_APK,)

    def rn(self, source):
        root = self.root / "rn"
        if source is not None:
            write(root / HERMES_EXECUTOR_PATH, source)
        return root

    def verify(self, rn_root, libs, abi="arm64-v8a"):
        apk = make_apk(self.root / "candidate.apk", libs)
        return self.run_cmd(["python3", "-I", support.VERIFY_APK, "native-libs", "--apk", apk, "--rn-root", rn_root,
                             "--abi", abi])

    def test_rn086_hermesvm_without_separate_executor_passes(self):
        # The real beta.1 APK shape: libhermesvm.so + libreactnative.so, no libhermes_executor.so.
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_RN086), ["libhermesvm.so", *COMMON_LIBS])
        self.assertEqual(rc, 0, out)

    def test_rn086_missing_engine_fails(self):
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_RN086), COMMON_LIBS)
        self.assertEqual(rc, 4, out)

    def test_rn086_legacy_engine_name_does_not_satisfy(self):
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_RN086), ["libhermes.so", *COMMON_LIBS])
        self.assertEqual(rc, 4, out)

    def test_missing_libreactnative_fails(self):
        libs = ["libhermesvm.so"] + [lib for lib in COMMON_LIBS if lib != "libreactnative.so"]
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_RN086), libs)
        self.assertEqual(rc, 4, out)

    def test_legacy_hermes_fixture_passes(self):
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_LEGACY), ["libhermes.so", *COMMON_LIBS])
        self.assertEqual(rc, 0, out)

    def test_legacy_fixture_rejects_hermesvm_only_archive(self):
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_LEGACY), ["libhermesvm.so", *COMMON_LIBS])
        self.assertEqual(rc, 4, out)

    def test_unknown_engine_fails_closed(self):
        src = HERMES_EXECUTOR_RN086.replace('loadLibrary("hermesvm")', 'loadLibrary("hermes_v2")')
        rc, out = self.verify(self.rn(src), ["libhermes_v2.so", "libhermesvm.so", "libhermes.so", *COMMON_LIBS])
        self.assertNotEqual(rc, 0, out)

    def test_ambiguous_engines_fail_closed(self):
        src = HERMES_EXECUTOR_RN086.replace('SoLoader.loadLibrary("hermes_executor")',
                                            'SoLoader.loadLibrary("hermes")\n        SoLoader.loadLibrary("hermes_executor")')
        rc, out = self.verify(self.rn(src), ["libhermes.so", "libhermesvm.so", *COMMON_LIBS])
        self.assertNotEqual(rc, 0, out)

    def test_missing_executor_source_fails_closed(self):
        rc, out = self.verify(self.rn(None), ["libhermesvm.so", *COMMON_LIBS])
        self.assertNotEqual(rc, 0, out)

    def test_unparseable_executor_source_fails_closed(self):
        rc, out = self.verify(self.rn("package com.facebook.hermes.reactexecutor\n// no loader calls\n"),
                              ["libhermesvm.so", *COMMON_LIBS])
        self.assertNotEqual(rc, 0, out)

    def test_foreign_abi_entry_fails(self):
        libs = ["libhermesvm.so", *COMMON_LIBS, "lib/x86_64/libreactnative.so"]
        rc, out = self.verify(self.rn(HERMES_EXECUTOR_RN086), libs)
        self.assertEqual(rc, 4, out)


if __name__ == "__main__":
    import unittest
    unittest.main()
