#!/bin/bash
# Copy one built APK, its build manifest and checksum into the local Nextcloud sync folder.
#   scripts/upload-nextcloud.sh --apk FILE --manifest FILE [--root DIR] [--subdir Paseo/mobile] [--dry-run]
# Defaults: --root is the local Nextcloud folder, --subdir Paseo/mobile. Never overwrites, never leaves the root,
# never touches the Paseo daemon. A local copy does not prove the server has synced it.
# Exit codes: 0 ok, 2 usage, 3 precondition or write failure, 4 verification failure, 5 refused overwrite.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd -P)
exec python3 -I "$HERE/mobile-build/upload_nextcloud.py" "$@"
