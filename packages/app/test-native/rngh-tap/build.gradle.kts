// Dev-only JVM regression harness for the patched react-native-gesture-handler TapGestureHandler (Robolectric).
// Not part of the app build. Compiles the REAL RNGH sources copied from the patched install at build time.
plugins {
  id("com.android.library") version "8.7.2"
  id("org.jetbrains.kotlin.android") version "2.1.20"
}

// Default: the repository's patched install. -PrnghSource=<dir> points at another patched copy of the package.
val rnghSource = file(providers.gradleProperty("rnghSource").getOrElse("../../../../node_modules/react-native-gesture-handler"))
val rnghCopy = layout.buildDirectory.dir("rngh-src")

val syncRngh by tasks.registering(Sync::class) {
  from(rnghSource.resolve("android/src/main/java")) {
    include("com/swmansion/gesturehandler/core/**")
    include("com/swmansion/gesturehandler/react/Extensions.kt")
    include("com/swmansion/gesturehandler/react/RNGestureHandlerTouchEvent.kt")
    include("com/swmansion/gesturehandler/react/eventbuilders/**")
  }
  from(rnghSource.resolve("android/nosvg/src/main/java")) { include("com/swmansion/gesturehandler/RNSVGHitTester.kt") }
  into(rnghCopy)
  doFirst { require(rnghSource.resolve("android/src/main/java").isDirectory) { "RNGH source not found: $rnghSource" } }
}

android {
  namespace = "com.swmansion.gesturehandler"
  compileSdk = 35
  defaultConfig { minSdk = 24 }
  buildFeatures { buildConfig = true }
  compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
  sourceSets["main"].java.setSrcDirs(listOf(rnghCopy.get().asFile, file("test-shims")))
}
kotlin { jvmToolchain(17) }
tasks.named("preBuild") { dependsOn(syncRngh) }

dependencies {
  compileOnly("com.facebook.react:react-android:0.86.3")
  testImplementation("com.facebook.react:react-android:0.86.3")
  testImplementation("junit:junit:4.13.2")
  testImplementation("org.robolectric:robolectric:4.14.1")
}
