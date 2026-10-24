"""Check Firebase string resources in a built APK. Prints only presence/equality booleans; exits 1 on mismatch.

  firebase_resources.py json <aapt2> <google-services.json> <package> <built.apk>
      Built resources must equal the values the google-services plugin derives from the JSON.
  firebase_resources.py apk <aapt2> <reference.apk> <built.apk>
      Built resources must equal the reference APK's resources.

Each resource block in `aapt2 dump resources` runs from its `resource 0x... string/<name>` header to the
next header. Only value rows inside the block, `(<config>) "<value>"`, are compared, keyed by config.
Resource IDs and neighboring resources never enter the comparison.
"""
import json
import re
import subprocess
import sys

NAMES = ["gcm_defaultSenderId", "google_api_key", "google_app_id", "google_crash_reporting_api_key", "google_storage_bucket", "project_id"]
HEADER = re.compile(r"^(resource 0x[0-9a-fA-F]+ |type |Package name=)")
STRING_HEADER = re.compile(r"^resource 0x[0-9a-fA-F]+ string/(\w+)$")
VALUE = re.compile(r'^\(([^)]*)\)\s+"(.*)"$')


def parse(text):
    """Map wanted string name -> {config: value}."""
    found = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if HEADER.match(line):
            m = STRING_HEADER.match(line)
            current = m.group(1) if m and m.group(1) in NAMES else None
            if current is not None:
                found.setdefault(current, {})
            continue
        if current is not None:
            v = VALUE.match(line)
            if v:
                found[current][v.group(1)] = v.group(2)
    return found


def from_google_services(path, package):
    """Default-config values the google-services Gradle plugin generates for `package`."""
    with open(path) as f:
        data = json.load(f)
    clients = [c for c in data["client"] if c["client_info"]["android_client_info"]["package_name"] == package]
    if len(clients) != 1:
        raise SystemExit(f"expected exactly one client for package, found {len(clients)}")
    client, info = clients[0], data["project_info"]
    key = client["api_key"][0]["current_key"]
    return {
        "gcm_defaultSenderId": {"": info["project_number"]},
        "google_api_key": {"": key},
        "google_app_id": {"": client["client_info"]["mobilesdk_app_id"]},
        "google_crash_reporting_api_key": {"": key},
        "google_storage_bucket": {"": info["storage_bucket"]},
        "project_id": {"": info["project_id"]},
    }


def compare(expected, built):
    """Return (ok, rows) without exposing values."""
    rows = []
    ok = True
    for n in NAMES:
        ev, bv = expected.get(n, {}), built.get(n, {})
        nonempty = bool(ev) and bool(bv) and all(ev.values()) and all(bv.values())
        equal = nonempty and ev == bv
        ok = ok and equal
        rows.append(f"{n}: expected_present={bool(ev)} built_present={bool(bv)} configs={len(ev)}/{len(bv)} nonempty={nonempty} equal={equal}")
    return ok, rows


def dump(aapt2, apk):
    return subprocess.run([aapt2, "dump", "resources", apk], check=True, capture_output=True, text=True).stdout


def main(argv):
    if len(argv) == 5 and argv[0] == "json":
        _, aapt2, gs, package, built = argv
        expected = from_google_services(gs, package)
    elif len(argv) == 4 and argv[0] == "apk":
        _, aapt2, reference, built = argv
        expected = parse(dump(aapt2, reference))
    else:
        print(__doc__)
        return 2
    ok, rows = compare(expected, parse(dump(aapt2, built)))
    print("\n".join(rows))
    print(f"firebase_{argv[0]}_all_equal={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
