# rngh-tap: TapGestureHandler mouse/touchpad regression harness

Dev-only JVM tests (Robolectric) for the `TapGestureHandler` change in
`patches/react-native-gesture-handler+2.32.0.patch`. Not part of the app build and adds no app dependency.
The build copies the real patched RNGH sources (`core/`, `react/Extensions.kt`, `react/RNGestureHandlerTouchEvent.kt`,
`react/eventbuilders/`, nosvg `RNSVGHitTester.kt`) from `node_modules` into `build/rngh-src` and compiles them;
nothing from RNGH is checked in.

## Run

```sh
cd packages/app/test-native/rngh-tap
echo "sdk.dir=$ANDROID_HOME" > local.properties   # Android SDK with platform 35
JAVA_HOME=<JDK 17> gradle testDebugUnitTest        # Gradle 8.14.x
# against another patched copy of the package:
JAVA_HOME=<JDK 17> gradle testDebugUnitTest -PrnghSource=<dir>/react-native-gesture-handler
```

Run it after `npm install` has applied the patch. The first run downloads Robolectric's Android 12 (SDK 31)
`android-all` jar from Maven Central.

Pins: Gradle 8.14.3, AGP 8.7.2, Kotlin 2.1.20, Robolectric 4.14.1, JUnit 4.13.2, `react-android` 0.86.3
(the app's React Native), RNGH 2.32.0, Robolectric SDK 31.

## What it covers

18 tests drive a real `TapGestureHandler` through a real `GestureHandlerOrchestrator` with real `MotionEvent`s:
touchpad `DOWN`/`UP` with `PRIMARY` and no `BUTTON_*`, full USB mouse streams, `numberOfTaps` 2, primary and secondary
buttons, button-only streams, stray `UP`, cancel, drag beyond `maxDist`, drag outside the view, finger taps.

## Limits

- `test-shims/ReactShims.kt` replaces `RNGestureHandlerRootView`, `RNGestureHandlerRootHelper.RootViewGestureHandler`
  and `RNGestureHandlerButtonViewManager.ButtonViewGroup` with empty types: `core/` uses them only by type or
  identity, and the real classes need generated codegen. Root-view nesting, root helper and button views are not
  exercised.
- No React root, no hover, no real input pipeline or device; Android framework behaviour is Robolectric SDK 31.

## Mutation controls (manual)

In `build/rngh-src/.../core/TapGestureHandler.kt` after a sync, replace
`if (mouseBoundary == MouseBoundary.TOUCH) false else null` with `true`: the USB `numberOfTaps` 2 and secondary-click
cases fail. Replace `val mouseDecision = mouseClickBoundary(sourceEvent)` with `val mouseDecision: Boolean? = null`:
the touchpad cases fail. Run `gradle testDebugUnitTest -x syncRngh` so the edit is not overwritten.
