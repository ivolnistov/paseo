# Provenance

`scripts/build-apk.sh` derives from the audited 0.11.2 helper `paseo-local-build.sh` (sha256
`2cba119e4f82ed2e55d6d2a7c8361dc1c796a0c2c1e81cd3dda9cd9b8daeebea`, from the local build bundle's `SHA256SUMS`). The
audited bundle is kept unchanged outside the repo.

`payload_compare.py` (sha256 `4abbd409f2f2ab27ffb263964f330a8e48126668f9f30774e36a710e3421e305`) and
`firebase_resources.py` (sha256 `ead2aacbbda048997f5a13b5e178be7a3050cc577103d23e3cfd6e26769812a2`) are byte copies of
the audited files; both digests equal the audited bundle's `SHA256SUMS`, and the copies are `cmp`-identical.

## Changes from the audited helper

- Inputs are explicit flags: `--src`, `--expected-sha`, `--version`, `--variant`, `--abi`, `--keystore` (directory),
  `--out`, `--java-home` and `--android-home`. No default points at a past source, commit, version or output.
- versionName and versionCode come from `packages/app/native-release-version.js` instead of flags.
- SDK build-tools, NDK and compileSdk come from the installed React Native version catalog. Preflight also checks the
  NDK and platform.
- The terminal-webview preparation step is gone. The supported sources (v0.13.0-beta.2 and later, including v0.13.0)
  removed the WebView terminal renderer; the tools commit's own base is irrelevant, because every gate follows the
  explicit `--src`.
- `libhermes.so` is no longer hardcoded. `verify_apk.py native-libs` reads the engine from the installed
  `HermesExecutor.kt` and fails closed on an unknown or ambiguous engine. RN 0.86 loads `hermesvm`; `hermes_executor`
  is merged and not required as its own file.
- The required DEX classes and Expo registry entries follow the modules the source declares, including
  `ScreenOrientationModule` when `expo-screen-orientation` is a dependency.
- Prebuild validation adds gates for config plugin output:
  - the keyboard `dispatchKeyEvent` hook in MainActivity
  - `smallestScreenSize` in MainActivity configChanges
  - no portrait lock when app.config declares `orientation: "default"`
- Dependencies: `deps-state.sh` owns the `npm ci` decision and the `deps.ok` marker, and can adopt a recorded
  successful install receipt.
- The lock moved to `<state-dir>/build.lock` (`lock.sh`). A stale lock is reported and refused, not taken over.
- The keystore certificate is checked in preflight with `keytool -storepass:file`, before any build step.
- Delivery writes artifact-specific `<apk>.sha256` and `<apk>.build-manifest.json`, with no global SHA256SUMS. Every
  file is placed without replacement.
- The manifest records caller-supplied input digests (`--provenance NAME=SHA256`, repeatable, conflicting
  duplicates refused) and the generated Gradle wrapper identity (distribution URL and SHA-256, properties and jar
  hashes), read without invoking Gradle.
- `--version` must equal the source `packages/app/package.json` version. `PASEO_FDROID_BUILD` and
  `PASEO_PROFILE_BUILD`, which app.config and Metro read, are part of the prebuild and bundle fingerprints. An
  existing identical delivery keeps its manifest only when the build identity matches, ignoring run metadata.
- Variants are explicit: production (sh.paseo, `GOOGLE_SERVICES_FILE_PROD`) and development (sh.paseo.debug,
  `GOOGLE_SERVICES_FILE_DEBUG`). The env key reaches both prebuild and Gradle. Other variants and mismatched
  packages are refused, and a source variants table must declare the pair. `PASEO_WATCHDOG_BUILD`,
  `PASEO_DIAGNOSTIC_REVISION` and `PASEO_DIAGNOSTIC_BUILD_TIME` join the stamp env. A failing build or verifier tool
  exits 4.
- SDK pins are tentative before dependencies are installed and re-read from the installed catalog after the deps
  step, where a missing or incomplete catalog or a missing SDK path exits 3. After deps, the entry outputs of the
  app's `@getpaseo/*` workspace dependencies must exist (`app_deps_outputs.py`), and their hashes feed the bundle
  fingerprint. `--out`/`--state-dir` inside the source must be git-ignored. A per-checkout lock in the worktree's
  git dir is taken before the state lock. `--with-jvm-tests` requires the keyboard Android module. `adopt` always
  validates the receipt.
- Exit codes: 2 usage, 3 precondition, 4 build or verification failed, 5 refused overwrite.
