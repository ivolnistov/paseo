const { withAppBuildGradle } = require("expo/config-plugins");

// Expo derives Android versionName from the shared `version`, which iOS needs numeric. An owned fork build sets
// only the Android versionName to its full name; build-apk.sh verifies the resulting build.gradle and APK.
function withAndroidVersionName(config, versionName) {
  if (!versionName) return config;
  return withAppBuildGradle(config, (modConfig) => {
    const pattern = /versionName ".*"/;
    if (!pattern.test(modConfig.modResults.contents)) {
      throw new Error("Could not set the Android versionName: no versionName in app/build.gradle");
    }
    modConfig.modResults.contents = modConfig.modResults.contents.replace(
      pattern,
      `versionName "${versionName}"`,
    );
    return modConfig;
  });
}

module.exports = withAndroidVersionName;
