package com.swmansion.gesturehandler.core

import android.os.Looper
import android.os.SystemClock
import android.view.InputDevice
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import org.robolectric.RuntimeEnvironment
import com.facebook.react.bridge.JavaOnlyMap
import com.facebook.react.uimanager.DisplayMetricsHolder
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/** Boundary cases. Real TapGestureHandler + GestureHandlerOrchestrator, real MotionEvents (Robolectric). Counts ACTIVE transitions. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [31])
class TapGestureHandlerMouseBoundaryTest {
  private val context = RuntimeEnvironment.getApplication()
  private val root = FrameLayout(context)
  private val target = View(context)
  private var activations = 0
  private var downTime = 0L

  private fun tap(config: JavaOnlyMap = JavaOnlyMap()): GestureHandlerOrchestrator {
    DisplayMetricsHolder.initDisplayMetricsIfNotInitialized(context)
    root.layout(0, 0, 400, 400)
    target.layout(0, 0, 400, 400)
    if (target.parent == null) root.addView(target)
    target.layout(0, 0, 400, 400)
    val handler = TapGestureHandler()
    TapGestureHandler.Factory().setConfig(handler, config)
    handler.tag = 1
    handler.onTouchEventListener = object : OnTouchEventListener {
      override fun <T : GestureHandler> onHandlerUpdate(handler: T, event: MotionEvent) {}
      override fun <T : GestureHandler> onStateChange(handler: T, newState: Int, oldState: Int) {
        if (newState == GestureHandler.STATE_ACTIVE) activations++
      }
      override fun <T : GestureHandler> onTouchEvent(handler: T) {}
    }
    val registry = object : GestureHandlerRegistry {
      override fun getHandlersForView(view: View) = if (view === target) arrayListOf<GestureHandler>(handler) else null
    }
    val config2 = object : ViewConfigurationHelper {
      override fun getPointerEventsConfigForView(view: View) = PointerEventsConfig.AUTO
      override fun isViewClippingChildren(view: ViewGroup) = false
    }
    return GestureHandlerOrchestrator(root, registry, config2, root)
  }

  private fun ev(o: GestureHandlerOrchestrator, action: Int, x: Float, y: Float, buttonState: Int, actionButton: Int = 0,
                 tool: Int = MotionEvent.TOOL_TYPE_MOUSE) {
    val now = SystemClock.uptimeMillis()
    if (action == MotionEvent.ACTION_DOWN) downTime = now
    val p = MotionEvent.PointerProperties().apply { id = 0; toolType = tool }
    val c = MotionEvent.PointerCoords().apply { this.x = x; this.y = y; pressure = 1f; size = 1f }
    val source = if (tool == MotionEvent.TOOL_TYPE_MOUSE) InputDevice.SOURCE_MOUSE else InputDevice.SOURCE_TOUCHSCREEN
    val e = MotionEvent.obtain(downTime, now, action, 1, arrayOf(p), arrayOf(c), 0, buttonState, 1f, 1f, 0, 0, source, 0)
    if (actionButton != 0) MotionEvent::class.java.getMethod("setActionButton", Int::class.java).invoke(e, actionButton)
    o.onTouchEvent(e)
    e.recycle()
    shadowOf(Looper.getMainLooper()).idle()
  }

  private val P = MotionEvent.BUTTON_PRIMARY
  private val S = MotionEvent.BUTTON_SECONDARY

  private fun touchpadClick(o: GestureHandlerOrchestrator, button: Int = P, x: Float = 100f, y: Float = 100f) {
    ev(o, MotionEvent.ACTION_DOWN, x, y, button); ev(o, MotionEvent.ACTION_UP, x, y, 0)
  }
  private fun usbClick(o: GestureHandlerOrchestrator, button: Int = P) {
    ev(o, MotionEvent.ACTION_DOWN, 100f, 100f, button)
    ev(o, MotionEvent.ACTION_BUTTON_PRESS, 100f, 100f, button, button)
    ev(o, MotionEvent.ACTION_BUTTON_RELEASE, 100f, 100f, 0, button)
    ev(o, MotionEvent.ACTION_UP, 100f, 100f, 0)
  }

  @Test fun `numberOfTaps 2 after a cancelled touchpad click, two genuine clicks activate once`() {
    val o = tap(JavaOnlyMap.of("numberOfTaps", 2))
    ev(o, MotionEvent.ACTION_DOWN, 100f, 100f, P); ev(o, MotionEvent.ACTION_CANCEL, 100f, 100f, 0)
    touchpadClick(o); touchpadClick(o)
    assertEquals(1, activations)
  }
  @Test fun `configured secondary touchpad stream (DOWN UP with SECONDARY) activates once`() {
    val o = tap(JavaOnlyMap.of("mouseButton", S)); touchpadClick(o, S); assertEquals(1, activations)
  }
  @Test fun `mouse drag outside the view does not activate`() {
    val o = tap()
    ev(o, MotionEvent.ACTION_DOWN, 100f, 100f, P); ev(o, MotionEvent.ACTION_MOVE, 500f, 100f, P); ev(o, MotionEvent.ACTION_UP, 500f, 100f, 0)
    assertEquals(0, activations)
  }
  @Test fun `finger drag beyond max distance does not activate`() {
    val o = tap(JavaOnlyMap.of("maxDist", 8.0))
    ev(o, MotionEvent.ACTION_DOWN, 100f, 100f, 0, tool = MotionEvent.TOOL_TYPE_FINGER)
    ev(o, MotionEvent.ACTION_MOVE, 140f, 100f, 0, tool = MotionEvent.TOOL_TYPE_FINGER)
    ev(o, MotionEvent.ACTION_UP, 140f, 100f, 0, tool = MotionEvent.TOOL_TYPE_FINGER)
    assertEquals(0, activations)
  }
}
