"""APK gates that depend on the installed React Native version.

  python3 -I verify_apk.py native-libs --apk FILE --rn-root DIR --abi ABI

The JS engine library comes from <rn-root>/.../HermesExecutor.kt, not from a hardcoded name. React Native 0.86
loads the standalone engine `hermesvm` and the executor `hermes_executor`, which is merged into another archive
and does not ship as its own .so. Older versions loaded `hermes`. Exactly one supported engine must be loaded;
an unknown loader name, two engines, a missing file or no loader calls fail closed.

Exit codes: 0 pass, 2 usage, 3 cannot determine the engine (fail closed), 4 APK fails the gate.
"""
import argparse
import pathlib
import re
import sys
import zipfile

HERMES_EXECUTOR = "ReactAndroid/src/main/java/com/facebook/hermes/reactexecutor/HermesExecutor.kt"
ENGINES = {"hermes", "hermesvm"}
# Loaded by name but merged into libreactnative.so or another archive by the RN build.
MERGED = {"hermes_executor"}
LOADER = re.compile(r'SoLoader\.loadLibrary\(\s*"([A-Za-z0-9_]+)"\s*\)')


def engine_from_source(rn_root):
    path = pathlib.Path(rn_root) / HERMES_EXECUTOR
    try:
        text = path.read_text()
    except OSError as e:
        raise LookupError(f"cannot read {path}: {e}")
    names = set(LOADER.findall(text))
    if not names:
        raise LookupError(f"no SoLoader.loadLibrary calls in {path}")
    unknown = names - ENGINES - MERGED
    if unknown:
        raise LookupError(f"unknown native library names in {path}: {sorted(unknown)}")
    engines = names & ENGINES
    if len(engines) != 1:
        raise LookupError(f"expected exactly one JS engine in {path}, found {sorted(engines) or 'none'}")
    return engines.pop()


def native_libs(args):
    try:
        engine = engine_from_source(args.rn_root)
    except LookupError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 3
    try:
        with zipfile.ZipFile(args.apk) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile) as e:
        print(f"FAIL: cannot read {args.apk}: {e}", file=sys.stderr)
        return 4
    libs = [n for n in names if n.startswith("lib/")]
    foreign = sorted({n.split("/")[1] for n in libs if len(n.split("/")) > 2} - {args.abi})
    failures = []
    if foreign:
        failures.append(f"lib/ has other ABIs: {foreign}")
    for lib in (f"lib{engine}.so", "libreactnative.so"):
        if f"lib/{args.abi}/{lib}" not in names:
            failures.append(f"missing lib/{args.abi}/{lib}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 4
    print(f"PASS: engine lib{engine}.so (from installed HermesExecutor.kt) and libreactnative.so, {args.abi} only")
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="verify_apk.py")
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("native-libs")
    n.add_argument("--apk", required=True)
    n.add_argument("--rn-root", required=True)
    n.add_argument("--abi", required=True)
    args = p.parse_args(argv)
    return native_libs(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
