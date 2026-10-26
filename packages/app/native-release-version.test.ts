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
  // Root policy (CF option 2): fork version codes come from an explicit committed allocation table, not a formula.
  // The counter reset to 0.13.1-fork.1, which must install over the already-shipped 0.13.0-fork.4 (13004). Any fork
  // version without an allocation fails closed. The numeric version stays numeric for iOS and the shared config; the
  // full fork name is the returned Android-only `androidVersionName`.
  it("0.13.1-fork.1 (counter reset) maps to allocated code 13005, above the installed 0.13.0-fork.4", () => {
    const reset = getNativeReleaseVersion("0.13.1-fork.1");
    expect(reset).toEqual({
      appVersion: "0.13.1",
      androidVersionCode: 13005,
      androidVersionName: "0.13.1-fork.1",
      iosBuildNumber: "13001999",
    });
    expect(reset.androidVersionCode).toBeGreaterThan(
      getNativeReleaseVersion("0.13.0-fork.4").androidVersionCode,
    );
  });

  it("historical 0.13.0 fork releases keep their shipped codes", () => {
    for (const [fork, code] of [
      [2, 13002],
      [3, 13003],
      [4, 13004],
    ]) {
      expect(getNativeReleaseVersion(`0.13.0-fork.${fork}`)).toEqual({
        appVersion: "0.13.0",
        androidVersionCode: code,
        androidVersionName: `0.13.0-fork.${fork}`,
        iosBuildNumber: "13000999",
      });
    }
  });

  it("rejects fork versions without an explicit allocation instead of deriving a code", () => {
    // 0.13.0-fork.5 is what the old formula would hand 13005; 0.13.1-fork.2 is the next unallocated reset build.
    for (const version of ["0.13.0-fork.5", "0.13.1-fork.2", "0.13.2-fork.1", "0.14.0-fork.1"]) {
      expect(() => getNativeReleaseVersion(version)).toThrow(/allocat/i);
    }
  });

  it("still rejects a fork number of 0 and forks past the patch slot", () => {
    expect(() => getNativeReleaseVersion("0.13.0-fork.0")).toThrow();
    expect(() => getNativeReleaseVersion("0.13.998-fork.2")).toThrow();
    // Formerly derived 13999; with explicit allocation an in-range but unallocated fork fails closed too.
    expect(() => getNativeReleaseVersion("0.13.997-fork.2")).toThrow(/allocat/i);
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
    expect(getNativeReleaseVersion("0.13.1")).toEqual({
      appVersion: "0.13.1",
      androidVersionCode: 13001,
      iosBuildNumber: "13001999",
    });
    expect(
      getFdroidVersionCodes("0.13.1").map((entry: { versionCode: number }) => entry.versionCode),
    ).toEqual([130011, 130012, 130013, 130014]);
  });
});
