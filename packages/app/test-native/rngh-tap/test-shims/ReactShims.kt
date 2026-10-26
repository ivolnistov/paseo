// TEST-ONLY compile shims for codegen-dependent RNGH react classes that core/ references only by type or identity.
// No gesture logic: a plain non-RNGH root view, no RootViewGestureHandler instances, no ButtonViewGroup instances.
package com.swmansion.gesturehandler.react

import android.content.Context
import android.view.ViewGroup

class RNGestureHandlerRootView(context: Context) : ViewGroup(context) {
  fun isRootViewEnabled() = false
  override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {}
}

class RNGestureHandlerRootHelper {
  interface RootViewGestureHandler
}

class RNGestureHandlerButtonViewManager {
  class ButtonViewGroup(context: Context) : ViewGroup(context) {
    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {}
  }
}
