---
name: paseo-mobile-build
description: Build a locally signed Paseo Android APK from a reviewed commit with scripts/build-apk.sh, and stage it into the local Nextcloud folder with scripts/upload-nextcloud.sh. Use for local release-style sideload builds; not for store releases (see release-beta/release-stable) or Expo dev builds.
---

# Paseo mobile build

Two entry points, `scripts/build-apk.sh` and `scripts/upload-nextcloud.sh`. Both need bash and python3, print
`--help` and use the same exit codes: 0 ok, 2 usage, 3 precondition, 4 build or verification failed, 5 refused
overwrite.

## Build

Build only from a clean worktree at an exact reviewed commit. Required inputs: `--src`, `--expected-sha`,
`--version`, `--variant`, `--abi`, `--google-services`, `--keystore`, `--expected-cert`, `--out`, `--java-home` and
`--android-home`. The script refuses a dirty tree, a different HEAD, a symlinked `node_modules`, a `--keystore`
`release.p12` or `storepass`, or a google-services file, with a mode other than 0600/0400, and a keystore whose
certificate differs from `--expected-cert`. It also refuses `--out` or `--state-dir` inside the source unless git
ignores that path (for example `.dev/`), and a second build of the same checkout while one runs.

```bash
scripts/build-apk.sh --src <worktree> --expected-sha <reviewed sha> --version 0.13.0 \
  --variant production --abi arm64-v8a --google-services <file> \
  --keystore ~/.android/keystores/paseo-local-release --expected-cert <sha256> \
  --java-home /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
  --android-home ~/Library/Android/sdk --out <new delivery dir> [--with-jvm-tests] [--dry-run] \
  [--provenance client.patch=<sha256> ...]
```

- Take `--expected-cert` from the owner's release record of the signing certificate, never from the keystore being
  checked; the build compares the keystore and the signed APK against it.
- `--variant` is production (sh.paseo, google-services as `GOOGLE_SERVICES_FILE_PROD`) or development
  (sh.paseo.debug, `GOOGLE_SERVICES_FILE_DEBUG`). Other variants are refused, and so is `PASEO_FDROID_BUILD=1`:
  F-Droid source builds are out of scope.
- Every gate follows the explicit `--src`: its plugins, modules and variants table. Supported sources are
  v0.13.0-beta.2 and later, which have no WebView terminal step. `--with-jvm-tests` needs
  `packages/app/modules/paseo-hardware-keyboard/android` in the source; preflight refuses it otherwise.
- Run `--dry-run` first. It runs preflight and prints the identity, SDK pins and each cache decision without
  writing anything.
- `--version` must equal `<src>/packages/app/package.json` exactly, prerelease suffix included.
  versionName and versionCode come from `<src>/packages/app/native-release-version.js`. A beta has the same
  versionCode as its stable release, so the version string in the APK name and manifest is the provenance.
- SDK build-tools, NDK and compileSdk come from the installed `react-native/gradle/libs.versions.toml`. On a cold
  checkout, preflight and the dry-run plan use tentative pins; after the deps step the installed catalog is read
  again and is authoritative, and a missing or incomplete catalog, or an SDK path it names that is not installed,
  exits 3 before prebuild. The manifest records where the pins came from. Cold preflight checks the tentative
  defaults (build-tools 36.0.0, NDK 27.1.12297006, android-36) before anything is installed, so a host without them
  is refused even if the source's catalog would name other versions. Tested with React Native 0.86 sources.
- A fresh checkout has no built workspace packages (`dist/` is ignored). After deps, the build checks the entry
  outputs of the app's `@getpaseo/*` dependencies and exits 3 if one is missing; pass `--with-app-deps` to build
  them. Presence and hashes do not prove the outputs match the workspace TypeScript sources: reuse only outputs
  built for the reviewed source, and pass `--with-app-deps` after changing workspace source. Their hashes feed the
  JS bundle fingerprint.
- Reuse, never clean:
  - Dependencies rerun `npm ci` only when the lockfile, workspace package.json files, `patches/`, the postinstall
    script, node, npm, platform or arch change, or when `deps.ok`, `node_modules` or its `.package-lock.json` is
    missing or changed. `deps.ok` is written only after a substantiated install.
  - `--deps-receipt FILE` adopts a recorded successful install of the same checkout. The receipt keys and the 17
    checks are listed in `scripts/mobile-build/deps-state.sh`. The receipt is checked on every run, even when
    `deps.ok` already matches; any mismatch stops the build instead of reinstalling silently.
  - Expo prebuild reruns only when its stamp changes. It keeps the existing `android/` and `.cxx`.
  - After prebuild, the generated project must contain the keyboard `dispatchKeyEvent` hook, the fold
    `smallestScreenSize` configChange and the orientation the source declares.
  - Gradle runs once, with one worker, no daemon and the build cache.
- Report cold or warm per stage from the plan output and the delivered manifest's `cache` block. An APK counts as
  built by a run only with that run's logs.
- The verifier derives the JS engine library from the installed `HermesExecutor.kt` (RN 0.86: `libhermesvm.so`).
  It checks signer, identity, ABI, the Hermes bundle, DEX classes and Expo registry entries for the declared
  modules, the signing-only payload difference, and Firebase resources.
- When the source carries applied patches or ports, pass each input's SHA-256 with a repeatable
  `--provenance NAME=SHA256`. The manifest records them under `provenance`. The manifest's
  `toolchain.gradleWrapper` records the wrapper's distribution URL and SHA-256 as generated.
- Two locks: one per checkout in its git dir (`paseo-mobile-build.lock`) and `<state-dir>/build.lock`. A refused
  stale lock (dead pid) is reported as stale and refused; clear it by hand only after proving that pid is dead and
  no build runs for that source.
- Ctrl-C signals the foreground process group, so the running step receives it too. A TERM sent to the build pid
  alone is handled after the current foreground step finishes; the locks stay held until the build exits.
- Delivery writes `<apk>`, `<apk>.sha256` and `<apk>.build-manifest.json` into `--out`. A differing existing file is
  never replaced. Use a new `--out` for a rebuild. An identical APK already in `--out` keeps its manifest only
  when every recorded field except run metadata (`cache`, `timings`, `workDir`) matches; otherwise exit 5.

Install, ADB, device, publication and daemon actions are out of scope for this skill.

## Stage into Nextcloud

```bash
scripts/upload-nextcloud.sh --apk <out>/<apk> --manifest <out>/<apk>.build-manifest.json --dry-run
```

- Defaults: the local Nextcloud folder, subdir `Paseo/mobile`. It writes the APK, `<apk>.build-manifest.json` and
  `<apk>.sha256`, after checking that the manifest names this APK with its SHA-256 and size and that `source.sha`
  is a full 40-hex commit id.
- It refuses `..`, absolute or symlinked paths and any differing existing file, including a different manifest for
  the same APK; stage a new receipt in a distinct `--subdir`. An identical rerun changes nothing. One upload per
  root runs at a time; a second one exits 3. An error or Ctrl-C removes only this run's files.
- `source.sha` is metadata the build recorded; it is not recovered from the APK.
- A real copy needs the owner's approval for that artifact. A local copy does not prove the server synced it.

Helpers, their tests and the derivation from the audited 0.11.2 helper are in `scripts/mobile-build/`
(see `PROVENANCE.md`).
