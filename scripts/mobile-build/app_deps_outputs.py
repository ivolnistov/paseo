"""JS entry outputs of the app's @getpaseo workspace dependencies, as installed in node_modules.

  python3 -I app_deps_outputs.py --src DIR

For each @getpaseo/* package in packages/app/package.json dependencies or devDependencies, resolve the installed
package (node_modules/@getpaseo/<name>, a workspace link) and its `main` and `exports` targets, skipping type-only
conditions. A glob target such as ./dist/*.js needs at least one match. These outputs are git-ignored build products
of `npm run build:app-deps`; a fresh checkout does not have them.

Prints the resolved output files, one per line, sorted. Exit codes: 0 all present, 3 something is missing (listed
on stderr), 2 usage.
"""
import argparse
import glob
import json
import os
import sys

TYPE_ONLY = {"types", "typings", "source", "react-native-types"}


def targets(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key not in TYPE_ONLY:
                targets(value, out)
    elif isinstance(node, list):
        for value in node:
            targets(value, out)


def main(argv):
    p = argparse.ArgumentParser(prog="app_deps_outputs.py")
    p.add_argument("--src", required=True)
    args = p.parse_args(argv)
    src = os.path.realpath(args.src)
    try:
        app = json.load(open(os.path.join(src, "packages/app/package.json")))
    except (OSError, ValueError) as e:
        print(f"cannot read packages/app/package.json: {e}", file=sys.stderr)
        return 3
    names = sorted({name for section in ("dependencies", "devDependencies")
                    for name in (app.get(section) or {}) if name.startswith("@getpaseo/")})
    found, missing = set(), []
    for name in names:
        pkg = os.path.join(src, "node_modules", name)
        try:
            manifest = json.load(open(os.path.join(pkg, "package.json")))
        except (OSError, ValueError):
            missing.append(f"{name}: not installed in node_modules")
            continue
        wanted = [manifest["main"]] if isinstance(manifest.get("main"), str) else []
        targets(manifest.get("exports"), wanted)
        for target in sorted(set(wanted)):
            path = os.path.normpath(os.path.join(pkg, target))
            matches = glob.glob(path) if "*" in target else ([path] if os.path.isfile(path) else [])
            if matches:
                found.update(os.path.realpath(m) for m in matches)
            else:
                missing.append(f"{name}: {target}")
    for path in sorted(found):
        print(path)
    if missing:
        for item in missing:
            print(f"missing workspace output {item}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
