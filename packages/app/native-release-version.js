const versionPattern = /^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+)|-fork\.(\d+))?$/;
const stableIosBuildSlot = 999;
// Owned fork builds (Android only) take their versionCode from this table, one entry per released fork version.
// It must only grow: a new entry needs a code above every shipped one, so the APK installs as an update. The fork
// counter can restart on a new upstream base (0.13.1-fork.1). A fork version without an entry is refused.
const FORK_ANDROID_VERSION_CODES = {
  "0.13.0-fork.2": 13002,
  "0.13.0-fork.3": 13003,
  "0.13.0-fork.4": 13004,
  "0.13.1-fork.1": 13005,
  "0.13.2-fork.1": 13006,
};
const FDROID_ABI_VERSION_CODE_SUFFIXES = {
  "armeabi-v7a": 1,
  "arm64-v8a": 2,
  x86: 3,
  x86_64: 4,
};

function getNativeReleaseVersion(version) {
  const match = versionPattern.exec(version);
  if (!match) {
    throw new Error(`Cannot derive native release version from unsupported version: ${version}`);
  }

  const [, majorText, minorText, patchText, betaText, forkText] = match;
  const major = Number(majorText);
  const minor = Number(minorText);
  const patch = Number(patchText);
  const betaNumber = betaText === undefined ? null : Number(betaText);
  const forkNumber = forkText === undefined ? null : Number(forkText);
  if (forkNumber !== null && (forkNumber < 1 || patch + forkNumber > 999)) {
    throw new Error(
      `Fork number must be at least 1 and keep the patch slot within 999: ${version}`,
    );
  }

  if (minor > 999 || patch > 999) {
    throw new Error(`Cannot derive collision-free native version from: ${version}`);
  }
  if (betaNumber !== null && (betaNumber < 1 || betaNumber >= stableIosBuildSlot)) {
    throw new Error(`iOS beta number must be between 1 and 998: ${version}`);
  }

  const baseCode = major * 1_000_000 + minor * 1_000 + patch;
  let versionCode = baseCode;
  if (forkNumber !== null) {
    if (!Object.hasOwn(FORK_ANDROID_VERSION_CODES, version)) {
      throw new Error(`No Android versionCode is allocated for fork version: ${version}`);
    }
    versionCode = FORK_ANDROID_VERSION_CODES[version];
  }
  if (
    !Number.isSafeInteger(versionCode) ||
    versionCode <= 0 ||
    versionCode * 10 + 9 > 2_100_000_000
  ) {
    throw new Error(`Derived Android versionCode is out of range: ${versionCode}`);
  }

  const iosBuildSlot = betaNumber ?? stableIosBuildSlot;
  const iosBaseCode = major * 1_000_000 + minor * 1_000 + patch; // forks are not iOS releases; iOS stays numeric
  const iosBuildNumber = iosBaseCode * 1_000 + iosBuildSlot;
  if (!Number.isSafeInteger(iosBuildNumber)) {
    throw new Error(`Derived iOS buildNumber is out of range: ${iosBuildNumber}`);
  }

  return {
    appVersion: `${major}.${minor}.${patch}`,
    androidVersionCode: versionCode,
    iosBuildNumber: String(iosBuildNumber),
    ...(forkNumber === null ? {} : { androidVersionName: version }),
  };
}

function getFdroidVersionCodes(version) {
  const { androidVersionCode } = getNativeReleaseVersion(version);
  return Object.entries(FDROID_ABI_VERSION_CODE_SUFFIXES).map(([abi, suffix]) => ({
    abi,
    versionCode: androidVersionCode * 10 + suffix,
  }));
}

module.exports = {
  FDROID_ABI_VERSION_CODE_SUFFIXES,
  getFdroidVersionCodes,
  getNativeReleaseVersion,
};
