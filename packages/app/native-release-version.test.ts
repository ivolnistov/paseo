import { describe, expect, it } from "vitest";

const {
  FDROID_ABI_VERSION_CODE_SUFFIXES,
  getFdroidVersionCodes,
  getNativeReleaseVersion,
} = require("./native-release-version");

describe("native release version", () => {
  it("reserves the final iOS build slot for a stable release", () => {
    expect(getNativeReleaseVersion("0.2.6")).toEqual({
      appVersion: "0.2.6",
      androidVersionCode: 2006,
      iosBuildNumber: "2006999",
    });
  });

  it("gives each beta a unique iOS build slot under the stable app version", () => {
    expect(getNativeReleaseVersion("0.2.6-beta.2")).toEqual({
      appVersion: "0.2.6",
      androidVersionCode: 2006,
      iosBuildNumber: "2006002",
    });
  });

  it("rejects beta numbers that consume the stable iOS build slot", () => {
    expect(() => getNativeReleaseVersion("0.2.6-beta.999")).toThrow(
      "iOS beta number must be between 1 and 998",
    );
  });

  it("derives one F-Droid version code per published ABI", () => {
    expect(FDROID_ABI_VERSION_CODE_SUFFIXES).toEqual({
      "armeabi-v7a": 1,
      "arm64-v8a": 2,
      x86: 3,
      x86_64: 4,
    });
    expect(getFdroidVersionCodes("0.5.0")).toEqual([
      { abi: "armeabi-v7a", versionCode: 50001 },
      { abi: "arm64-v8a", versionCode: 50002 },
      { abi: "x86", versionCode: 50003 },
      { abi: "x86_64", versionCode: 50004 },
    ]);
  });
});

describe("fork release versions", () => {
  // Root policy: owned fork build sequence 13003 for this APK (not globally collision-free: upstream 0.13.3 maps to
  // the same code; the fork ships on a different certificate/channel). The numeric version stays numeric for iOS
  // and the shared config; the full fork name is an Android-only versionName. `androidVersionName` is proposed.
  it("0.13.0-fork.3 maps to Android code 13003 with the full fork versionName; shared version stays numeric", () => {
    expect(getNativeReleaseVersion("0.13.0-fork.3")).toMatchObject({
      appVersion: "0.13.0",
      androidVersionCode: 13003,
      androidVersionName: "0.13.0-fork.3",
    });
  });

  it("rejects a fork number of 0 and a fork that would leave the patch slot", () => {
    expect(() => getNativeReleaseVersion("0.13.0-fork.0")).toThrow(/Fork number/);
    expect(() => getNativeReleaseVersion("0.13.998-fork.2")).toThrow(/Fork number/);
    expect(getNativeReleaseVersion("0.13.997-fork.2").androidVersionCode).toBe(13999);
  });

  it("stable and beta mappings are unchanged by fork support", () => {
    expect(getNativeReleaseVersion("0.13.0")).toEqual({
      appVersion: "0.13.0",
      androidVersionCode: 13000,
      iosBuildNumber: "13000999",
    });
    expect(getNativeReleaseVersion("0.13.0-beta.2")).toEqual({
      appVersion: "0.13.0",
      androidVersionCode: 13000,
      iosBuildNumber: "13000002",
    });
  });
});
