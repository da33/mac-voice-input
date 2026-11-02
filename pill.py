"""桌面浮動條（像 Wispr Flow 那顆小膠囊）。

平常是一條小小的膠囊；滑鼠移上去會提示怎麼用；錄音時展開成跟著聲音跳動的聲波；
辨識中變成流動的波浪。可以拖到任何位置，點一下打開設定選單。
這個視窗不會搶走焦點，所以文字還是會貼到你原本在打字的地方。
"""

from __future__ import annotations

import math
import time

import objc
from AppKit import (
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSInsetRect,
    NSMakePoint,
    NSMakeRect,
    NSPanel,
    NSPointInRect,
    NSScreen,
    NSStatusWindowLevel,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer, NSZeroRect

IDLE_SIZE = (28.0, 6.0)
HOVER_SIZE = (36.0, 9.0)  # 滑鼠移上去稍微變大，比較好拖
RECORDING_SIZE = (68.0, 24.0)
HANDSFREE_SIZE = (82.0, 24.0)
WORKING_SIZE = (44.0, 24.0)
TEXT_HEIGHT = 24.0
IDLE_ALPHA = 0.6

BAR_COUNT = 9
BAR_WIDTH = 2.0
BAR_GAP = 2.0

BACKGROUND = NSColor.colorWithSRGBRed_green_blue_alpha_(0.075, 0.075, 0.085, 1.0)
BORDER = NSColor.colorWithWhite_alpha_(1.0, 0.10)
RED = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.28, 0.24, 1.0)
TEXT_FONT = NSFont.systemFontOfSize_weight_(11.5, 0.23)


def _text(value: str, font, alpha: float) -> NSAttributedString:
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(1.0, alpha),
    }
    return NSAttributedString.alloc().initWithString_attributes_(value, attrs)


class PillView(NSView):
    def initWithPill_(self, pill):
        self = objc.super(PillView, self).initWithFrame_(NSMakeRect(0, 0, *IDLE_SIZE))
        if self is None:
            return None
        self.pill = pill
        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSZeroRect, options, self, None
        )
        self.addTrackingArea_(area)
        return self

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        self.pill.draw(self.bounds())

    def mouseEntered_(self, event):
        self.pill.set_hover(True)

    def mouseExited_(self, event):
        self.pill.set_hover(False)

    def mouseDown_(self, event):
        self.pill.mouse_down()

    def mouseDragged_(self, event):
        self.pill.mouse_dragged()

    def mouseUp_(self, event):
        self.pill.mouse_up()

    def rightMouseDown_(self, event):
        self.pill.on_click(self)


class FloatingPill:
    """mode：idle（待命）| recording | handsfree | working（辨識中）| text（顯示訊息）"""

    def __init__(self, on_click, on_moved, level_source):
        self.on_click = on_click  # on_click(view)
        self.on_moved = on_moved  # on_moved((center_x, bottom_y))
        self.level_source = level_source
        self.mode = "idle"
        self.text = ""
        self.hover = False
        self.show_when_idle = True
        self.anchor = self._default_anchor()
        self.size = list(IDLE_SIZE)
        self.alpha = 0.0
        self.levels = [0.0] * (BAR_COUNT // 2 + 1)
        self.smoothed = 0.0
        self.frame_count = 0
        self.timer = None
        self._drag = None

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, *IDLE_SIZE),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setAlphaValue_(0.0)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        self.view = PillView.alloc().initWithPill_(self)
        panel.setContentView_(self.view)
        self.panel = panel
        self._animate()

    # ---- 給主程式呼叫

    def set_mode(self, mode: str, text: str = "") -> None:
        if (mode, text) != (self.mode, self.text):
            self.mode, self.text = mode, text
            self._animate()

    def set_hint(self, hint: str) -> None:
        self.view.setToolTip_(hint)  # 滑鼠停一下才會出現的小提示，不會一直跳出來

    def set_show_when_idle(self, show: bool) -> None:
        self.show_when_idle = show
        self._animate()

    def set_anchor(self, anchor) -> None:
        if anchor and any(
            NSPointInRect(NSMakePoint(anchor[0], anchor[1] + 4), screen.frame())
            for screen in NSScreen.screens()
        ):
            self.anchor = (float(anchor[0]), float(anchor[1]))
        else:
            self.anchor = self._default_anchor()
        self._apply_frame()

    def set_hover(self, hover: bool) -> None:
        self.hover = hover
        self._animate()

    # ---- 滑鼠：拖曳移動、點一下開選單

    def mouse_down(self) -> None:
        self._drag = (NSEvent.mouseLocation(), self.anchor, False)

    def mouse_dragged(self) -> None:
        if self._drag is None:
            return
        start, anchor, moved = self._drag
        point = NSEvent.mouseLocation()
        dx, dy = point.x - start.x, point.y - start.y
        if moved or abs(dx) + abs(dy) > 3:
            self._drag = (start, anchor, True)
            self.anchor = (anchor[0] + dx, anchor[1] + dy)
            self._apply_frame()

    def mouse_up(self) -> None:
        drag, self._drag = self._drag, None
        if drag is not None and drag[2]:
            self.on_moved(self.anchor)
        else:
            self.on_click(self.view)

    # ---- 動畫

    def _target(self):
        if self.mode == "recording":
            return RECORDING_SIZE, 1.0
        if self.mode == "handsfree":
            return HANDSFREE_SIZE, 1.0
        if self.mode == "working":
            return WORKING_SIZE, 1.0
        if self.mode == "text":
            return (_text(self.text, TEXT_FONT, 1).size().width + 24, TEXT_HEIGHT), 1.0
        if self.hover:
            return HOVER_SIZE, 1.0
        return IDLE_SIZE, (IDLE_ALPHA if self.show_when_idle else 0.0)

    def _animate(self) -> None:
        if self.timer is None:
            self.timer = NSTimer.timerWithTimeInterval_repeats_block_(1 / 60, True, self._step)
            NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def _step(self, _timer) -> None:
        (target_w, target_h), target_alpha = self._target()
        w, h = self.size
        w += (target_w - w) * 0.3
        h += (target_h - h) * 0.3
        self.alpha += (target_alpha - self.alpha) * 0.25
        settled = abs(target_w - w) < 0.3 and abs(target_h - h) < 0.3 and abs(target_alpha - self.alpha) < 0.01
        if settled:
            w, h, self.alpha = target_w, target_h, target_alpha
        self.size = [w, h]

        if self.mode in ("recording", "handsfree"):
            level = min(1.0, max(0.0, (math.sqrt(self.level_source()) - 0.06) * 3.5))
            rate = 0.6 if level > self.smoothed else 0.25  # 聲音來時跳得快、退得慢
            self.smoothed += (level - self.smoothed) * rate
            self.frame_count += 1
            if self.frame_count % 2 == 0:
                self.levels = [self.smoothed] + self.levels[:-1]
        else:
            self.smoothed = 0.0
            self.levels = [0.0] * len(self.levels)

        self._apply_frame()
        self.panel.setAlphaValue_(self.alpha)
        if self.alpha > 0.01:
            self.panel.orderFrontRegardless()
        else:
            self.panel.orderOut_(None)
        self.view.setNeedsDisplay_(True)

        if settled and self.mode not in ("recording", "handsfree", "working"):
            self.timer.invalidate()
            self.timer = None

    def _apply_frame(self) -> None:
        center_x, bottom = self.anchor
        w, h = self.size
        self.panel.setFrame_display_(NSMakeRect(center_x - w / 2, bottom, w, h), True)
        self.panel.invalidateShadow()

    def _default_anchor(self):
        frame = NSScreen.screens()[0].visibleFrame()
        return (frame.origin.x + frame.size.width / 2, frame.origin.y + 14)

    # ---- 繪圖

    def draw(self, bounds) -> None:
        w, h = bounds.size.width, bounds.size.height
        radius = h / 2 - 0.5
        shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 0.5, 0.5), radius, radius
        )
        BACKGROUND.setFill()
        shape.fill()
        BORDER.setStroke()
        shape.setLineWidth_(1.0)
        shape.stroke()

        reveal = max(0.0, min(1.0, (h - 12) / 8))  # 膠囊長到夠高才畫內容，展開時不會擠成一團
        if reveal <= 0:
            return
        if self.mode in ("recording", "handsfree"):
            self._draw_recording(w, h, reveal)
        elif self.mode == "working":
            self._draw_working(w, h, reveal)
        elif self.mode == "text":
            self._draw_label(self.text, TEXT_FONT, 0.95 * reveal, w, h)

    def _draw_recording(self, w, h, reveal) -> None:
        mid = h / 2
        pulse = 0.7 + 0.3 * math.sin(time.monotonic() * 5)
        RED.colorWithAlphaComponent_(pulse * reveal).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(7, mid - 2.5, 5, 5)).fill()

        handsfree = self.mode == "handsfree"
        left, right = 16.0, w - (20.0 if handsfree else 8.0)
        total = BAR_COUNT * BAR_WIDTH + (BAR_COUNT - 1) * BAR_GAP
        x = left + (right - left - total) / 2
        NSColor.colorWithWhite_alpha_(1.0, 0.92 * reveal).setFill()
        center = BAR_COUNT // 2
        for i in range(BAR_COUNT):
            distance = abs(i - center)
            value = self.levels[distance] * (1 - distance * 0.07)
            bar_h = 2 + value * (h - 10)
            rect = NSMakeRect(x, mid - bar_h / 2, BAR_WIDTH, bar_h)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 1, 1).fill()
            x += BAR_WIDTH + BAR_GAP

        if handsfree:  # 右邊的小方塊：提示「再按一下（或點我）就結束」
            NSColor.colorWithWhite_alpha_(1.0, 0.8 * reveal).setFill()
            stop = NSMakeRect(w - 15, mid - 3, 6, 6)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(stop, 1.5, 1.5).fill()

    def _draw_working(self, w, h, reveal) -> None:
        count = 5
        total = count * BAR_WIDTH + (count - 1) * BAR_GAP
        x = (w - total) / 2
        t = time.monotonic()
        for i in range(count):
            wave = 0.5 + 0.5 * math.sin(t * 7 - i * 0.9)
            bar_h = 2 + wave * (h - 12)
            NSColor.colorWithWhite_alpha_(1.0, (0.45 + 0.5 * wave) * reveal).setFill()
            rect = NSMakeRect(x, h / 2 - bar_h / 2, BAR_WIDTH, bar_h)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 1, 1).fill()
            x += BAR_WIDTH + BAR_GAP

    def _draw_label(self, value, font, alpha, w, h) -> None:
        label = _text(value, font, alpha)
        size = label.size()
        label.drawAtPoint_(NSMakePoint((w - size.width) / 2, (h - size.height) / 2))
