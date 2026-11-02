#!/usr/bin/env python3
"""語音輸入：按住 Fn 講話，放開後把辨識出的文字直接貼到游標位置。

類似 Wispr Flow，但用 macOS 26 內建的蘋果語音辨識：免費、在本機跑、聲音不上傳。
  - 按住快捷鍵說話 → 放開 → 文字自動貼上
  - 輕按一下快捷鍵 → 免持錄音，再按一下（或點浮動條）結束；按 Esc 取消
桌面上有一條可以拖曳的浮動條，點它就能調整設定；選單列也有麥克風圖示。
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import objc
import Quartz
import sounddevice as sd
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSImage,
    NSMakePoint,
    NSBezelBorder,
    NSFont,
    NSMakeRect,
    NSScrollView,
    NSTextView,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardItem,
    NSPasteboardTypeString,
    NSSound,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from CoreFoundation import CFPreferencesCopyAppValue
from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSObject, NSProcessInfo
from PyObjCTools import AppHelper, MachSignals

from engine import (
    AUDIO_TIMEOUT,
    Recorder,
    RecorderHung,
    Transcriber,
    format_replacements,
    parse_replacements,
)
from pill import FloatingPill
from relaunch import CRASH_ENV

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.txt"
LOCK_PATH = APP_DIR / ".voice_type.lock"
APP_BUNDLE = APP_DIR / "語音輸入.app"
LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/local.voice-type.plist"

DEFAULT_CONFIG = {
    "hotkey": "fn",
    "microphone": "",
    "replacements": {
        "Dovince Resolve": "DaVinci Resolve",
        "Da Vinci Resolve": "DaVinci Resolve",
        "Whisper Flow": "Wispr Flow",
        "Wisper Flow": "Wispr Flow",
        "Cloud Call": "Claude Code",
    },
    "sounds": True,
    "show_pill": True,
    "pill_position": None,
    "restore_clipboard": True,
    "save_history": True,
    "tap_threshold_sec": 0.4,
}

# 快捷鍵名稱 → (keycode, 這顆鍵按住時 event flags 會出現的位元, 顯示名稱)
HOTKEYS = {
    "fn": (63, Quartz.kCGEventFlagMaskSecondaryFn, "Fn 🌐"),
    "right_option": (61, 0x40, "右 Option ⌥"),
    "left_option": (58, 0x20, "左 Option ⌥"),
    "right_command": (54, 0x10, "右 Command ⌘"),
    "right_shift": (60, 0x04, "右 Shift ⇧"),
    "right_control": (62, 0x2000, "右 Control ⌃"),
}
ESC_KEYCODE = 53
V_KEYCODE = 9
SYNTHETIC_TAG = 0x766F6963  # 標記自己送出的 ⌘V，免得被自己的鍵盤監聽當成「按了別的鍵」
MIN_SECONDS = 0.3  # 比這短的錄音當成誤觸
MAX_SECONDS = 600
SILENCE_PEAK = 0.005
# 麥克風卡住（例如睡眠喚醒、拔插麥克風後）時自動重開程式；記在環境變數裡，避免一直重開
RESTART_ENV = "VOICE_TYPE_AUTO_RESTART"
RESTART_COOLDOWN = 120
# 閃退監看（relaunch.py）的通知管道；記在環境變數裡，原地重開後還找得到
GUARD_ENV = "VOICE_TYPE_GUARD_FD"
APP_BUNDLE_ID = "local.voice-type"

SETTINGS_KEYBOARD = "x-apple.systempreferences:com.apple.Keyboard-Settings.extension"
SETTINGS_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if cfg["hotkey"] not in HOTKEYS:
        cfg["hotkey"] = DEFAULT_CONFIG["hotkey"]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def open_url(url: str) -> None:
    subprocess.Popen(["open", url])


# ---------------------------------------------------------------- 貼上


def _snapshot_clipboard(pb) -> list[dict]:
    items = []
    for item in pb.pasteboardItems() or []:
        data = {}
        for kind in item.types():
            value = item.dataForType_(kind)
            if value is not None:
                data[kind] = value
        items.append(data)
    return items


def _restore_clipboard(pb, items: list[dict]) -> None:
    pb.clearContents()
    restored = []
    for data in items:
        item = NSPasteboardItem.alloc().init()
        for kind, value in data.items():
            item.setData_forType_(value, kind)
        restored.append(item)
    if restored:
        pb.writeObjects_(restored)


def _send_cmd_v() -> None:
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, V_KEYCODE, key_down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, SYNTHETIC_TAG)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def deliver_text(text: str, restore_clipboard: bool) -> bool:
    """把文字貼到目前游標位置。沒有「輔助使用」權限時只複製到剪貼簿，回傳 False。"""
    pb = NSPasteboard.generalPasteboard()
    can_paste = bool(Quartz.CGPreflightPostEventAccess())
    saved = _snapshot_clipboard(pb) if (restore_clipboard and can_paste) else None

    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    if saved is not None:
        # 告訴剪貼簿管理工具這是暫時內容，不要記下來
        pb.setString_forType_("", "org.nspasteboard.TransientType")
    if not can_paste:
        return False

    _send_cmd_v()
    if saved is not None:
        change_count = pb.changeCount()

        def restore_later():
            if pb.changeCount() == change_count:  # 這段時間使用者沒有自己複製別的東西
                _restore_clipboard(pb, saved)

        AppHelper.callLater(0.8, restore_later)
    return True


class AppDelegate(NSObject):
    """選單按「結束」、登出、關機時，告訴閃退監看這是正常結束。"""

    def applicationWillTerminate_(self, notification):
        tell_guard_quitting()


# ---------------------------------------------------------------- 選單


class MenuBridge(NSObject):
    """讓 NSMenu 能呼叫 Python 函式：每個選項把要執行的函式放在 representedObject。"""

    def initWithBuilder_(self, builder):
        self = objc.super(MenuBridge, self).init()
        if self is None:
            return None
        self.builder = builder
        return self

    def menuNeedsUpdate_(self, menu):
        self.builder(menu)

    def fire_(self, sender):
        try:
            sender.representedObject()()
        except Exception as exc:
            print(f"選單動作錯誤：{exc!r}")


# ---------------------------------------------------------------- 主程式


class VoiceType:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.recorder = Recorder(cfg["microphone"])
        self.transcriber = Transcriber(
            cfg,
            on_state=lambda *state: AppHelper.callAfter(self._on_model_state, *state),
            on_done=lambda *result: AppHelper.callAfter(self._on_transcribed, *result),
        )

        self.state = "idle"  # idle | hold（按住中）| handsfree（免持錄音中）
        self.key_down = False
        self.ignore_release = False
        self.pressed_at = 0.0
        self.pending = 0
        self.ready = False
        self.was_ready = False
        self.load_error = None
        self.flash_text = ""
        self.flash_until = 0.0
        self.notice = ""  # 一直顯示到問題解決的提醒，例如缺權限
        self.last_text = ""
        self.tap = None
        self._icon = None
        self.restarting = False
        self.recovered = time.time() - env_time(RESTART_ENV) < 30  # 剛剛因為麥克風卡住自動重開過
        self.crash_recovered = time.time() - env_time(CRASH_ENV) < 30  # 剛剛閃退，被自動重新打開

        # 不讓 macOS 把背景程式降速（App Nap），否則按鍵、辨識、貼上都會變慢
        self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "語音輸入需要即時回應快捷鍵"
        )

        self.bridge = MenuBridge.alloc().initWithBuilder_(self.build_menu)
        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self.bridge)
        self.menu.setAutoenablesItems_(False)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.status_item.setMenu_(self.menu)

        self.pill = FloatingPill(
            on_click=self._pill_clicked,
            on_moved=self._pill_moved,
            level_source=lambda: self.recorder.level,
        )
        self.pill.set_anchor(cfg["pill_position"])
        self.pill.set_show_when_idle(cfg["show_pill"])
        self._apply_hotkey()

    def start(self) -> None:
        print(
            f"權限：輸入監控={bool(Quartz.CGPreflightListenEventAccess())}，"
            f"輔助使用={bool(Quartz.CGPreflightPostEventAccess())}"
        )
        self.transcriber.start()
        if not Quartz.CGPreflightPostEventAccess():
            Quartz.CGRequestPostEventAccess()
            print("⚠️  還沒有「輔助使用」權限：辨識結果會先複製到剪貼簿，要自己按 ⌘V 貼上。")
        self._install_hotkey_listener(first_try=True)
        self._refresh()

    # ---- 快捷鍵監聽

    def _install_hotkey_listener(self, first_try: bool = False) -> None:
        # 有「輔助使用」權限就能監聽按鍵（「輸入監控」不是必要的）
        if Quartz.CGPreflightPostEventAccess() or Quartz.CGPreflightListenEventAccess():
            mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) | Quartz.CGEventMaskBit(
                Quartz.kCGEventKeyDown
            )
            self.tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                self._on_event,
                None,
            )
        if self.tap is None:
            if first_try:
                print("⚠️  需要「輔助使用」權限才能偵測快捷鍵。")
            self.notice = "需要「輔助使用」權限・點我打開設定"
            AppHelper.callLater(2.0, self._install_hotkey_listener)  # 開好權限後自動接上，不用重開
            self._refresh()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        if self.notice:
            self.notice = ""
            self._flash("✅ 權限開好了，可以開始用了", 2.5)

    def _apply_hotkey(self) -> None:
        self.keycode, self.keymask, self.keyname = HOTKEYS[self.cfg["hotkey"]]
        self.key_down = False
        self.pill.set_hint(f"按住 {self.keyname} 說話・點我設定")

    def _fn_key_conflict(self) -> bool:
        """🌐 鍵如果還設定成切換輸入法／表情符號，按 Fn 錄音時也會觸發那些功能。"""
        if self.cfg["hotkey"] != "fn":
            return False
        value = CFPreferencesCopyAppValue("AppleFnUsageType", "com.apple.HIToolbox")
        return value is None or int(value) != 0

    def _on_event(self, proxy, event_type, event, refcon):
        try:
            if event_type in (
                Quartz.kCGEventTapDisabledByTimeout,
                Quartz.kCGEventTapDisabledByUserInput,
            ):
                Quartz.CGEventTapEnable(self.tap, True)
            elif Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == SYNTHETIC_TAG:
                pass
            elif event_type == Quartz.kCGEventFlagsChanged:
                keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if keycode == self.keycode:
                    down = bool(Quartz.CGEventGetFlags(event) & self.keymask)
                    if down != self.key_down:
                        self.key_down = down
                        self._hotkey_pressed() if down else self._hotkey_released()
            elif event_type == Quartz.kCGEventKeyDown:
                self._other_key_pressed(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                )
        except Exception as exc:  # 監聽器裡的例外不能往外丟，不然 macOS 會停用它
            print(f"鍵盤事件處理錯誤：{exc!r}")
        return event

    def _hotkey_pressed(self) -> None:
        if self.state == "idle":
            self._start_recording()
        elif self.state == "handsfree":
            self.ignore_release = True
            self._finish_recording()

    def _hotkey_released(self) -> None:
        if self.ignore_release:
            self.ignore_release = False
        elif self.state == "hold":
            if time.monotonic() - self.pressed_at < self.cfg["tap_threshold_sec"]:
                self.state = "handsfree"  # 只輕按一下：改成免持錄音，再按一下才結束
                self._refresh()
            else:
                self._finish_recording()

    def _other_key_pressed(self, keycode: int) -> None:
        if self.state == "hold":
            # 快捷鍵是被拿來按組合鍵（例如 Fn+方向鍵、⌥+字母），不是要講話
            self._cancel_recording(None)
        elif self.state == "handsfree" and keycode == ESC_KEYCODE:
            self._cancel_recording("已取消錄音")

    # ---- 錄音流程

    def _start_recording(self) -> None:
        if self.load_error is not None:
            self._flash("❌ 蘋果辨識沒辦法啟動，重試中…", 3)
            self.transcriber.reload()
            return
        if self.restarting:
            return
        try:
            self.recorder.start()
        except Exception as exc:
            print(f"麥克風打不開：{exc!r}")
            if isinstance(exc, RecorderHung) or (
                isinstance(exc, sd.PortAudioError) and time.time() - env_time(RESTART_ENV) > RESTART_COOLDOWN
            ):
                # 麥克風本身沒壞、是程式跟音訊系統的連線卡住了，重開程式就會恢復
                self._auto_restart()
            else:
                self._flash("⚠️ 麥克風打不開，檢查麥克風權限", 3)
            return
        self.state = "hold"
        self.pressed_at = time.monotonic()
        self._play("Tink")
        self._refresh()
        AppHelper.callLater(MAX_SECONDS, self._stop_if_too_long, self.pressed_at)

    def _stop_recorder(self):
        audio = self.recorder.stop()
        AppHelper.callLater(AUDIO_TIMEOUT + 0.5, self._check_audio_hung)
        return audio

    def _check_audio_hung(self) -> None:
        if self.recorder.hung() and not self.restarting:
            print("關麥克風沒有回應（macOS 音訊系統卡死了）")
            self._auto_restart()

    def _auto_restart(self) -> None:
        if self.restarting:
            return
        self.restarting = True
        self._flash("麥克風卡住了，自動重新啟動中…", 3)
        AppHelper.callLater(0.8, self._restart_process)

    def _restart_process(self) -> None:
        """原地重開（同一個程序），macOS 給「語音輸入」的權限會跟著保留。"""
        if self.pending:  # 上一句還在辨識，等它貼上再重開，才不會漏字
            AppHelper.callLater(0.3, self._restart_process)
            return
        print("🔄 麥克風卡住了，自動重新啟動語音輸入")
        helper = self.transcriber.apple.process
        self.transcriber.apple.stop()
        if helper is not None:
            helper.wait(timeout=2)
        os.environ[RESTART_ENV] = str(time.time())
        sys.stdout.flush()
        os.execv(sys.executable, sys.orig_argv)

    def _stop_if_too_long(self, started_at: float) -> None:
        if self.state != "idle" and self.pressed_at == started_at:
            self._finish_recording()

    def _finish_recording(self) -> None:
        audio, sample_rate = self._stop_recorder()
        self.state = "idle"
        seconds = len(audio) / sample_rate
        if seconds < MIN_SECONDS:
            pass
        elif np.abs(audio[int(0.25 * sample_rate) :]).max() < SILENCE_PEAK:  # 略過有些麥克風剛開啟時的爆音
            self._flash("沒聽到聲音，檢查一下麥克風", 2)
        else:
            self._play("Pop")
            self.pending += 1
            self.transcriber.jobs.put((audio, sample_rate, seconds))
        self._refresh()

    def _cancel_recording(self, message) -> None:
        self._stop_recorder()
        self.state = "idle"
        if message:
            self._play("Basso")
            self._flash(message, 1.2)
        self._refresh()

    def _on_transcribed(self, text: str, error, seconds: float, took: float, source: str, note) -> None:
        self.pending -= 1
        if error is not None:
            print(f"❌ 辨識失敗：{error!r}")
            self._flash("❌ 辨識失敗", 2)
        elif not text:
            self._flash("沒有聽到文字", 1.5)
        else:
            self.last_text = text
            pasted = deliver_text(text, self.cfg["restore_clipboard"])
            print(f"🎙  {seconds:.1f}s 錄音 → {took:.2f}s 辨識（{source}）：{text}")
            if not pasted:
                self._flash("已複製，按 ⌘V 貼上", 2)
            elif note:
                self._flash(note, 2.5)
            if self.cfg["save_history"]:
                with HISTORY_PATH.open("a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{text}\n")
        self._refresh()

    def _on_model_state(self, ready: bool, error, note) -> None:
        self.ready = ready
        self.load_error = error
        if error is not None:
            print(f"❌ 蘋果辨識沒辦法啟動：{error}")
        elif ready:
            print(f"✅ 準備好了！按住 {self.keyname} 說話，放開就會貼上文字。")
            if note:
                self._flash(note, 4)
            elif not self.was_ready:
                if self.recovered:
                    self._flash(f"✅ 麥克風恢復了，再按一次 {self.keyname}", 4)
                elif self.crash_recovered:
                    self._flash("✅ 剛剛閃退了，已經自動重新打開", 4)
                elif self._fn_key_conflict():
                    self._flash("🌐 鍵還會切換輸入法・點我看怎麼改", 6)
                else:
                    self._flash(f"按住 {self.keyname} 說話", 2.5)
            self.was_ready = True
        self._refresh()

    # ---- 畫面

    def _refresh(self) -> None:
        recording = self.state != "idle"
        if recording:
            self.pill.set_mode("handsfree" if self.state == "handsfree" else "recording")
        elif time.monotonic() < self.flash_until:
            self.pill.set_mode("text", self.flash_text)
        elif self.notice:
            self.pill.set_mode("text", self.notice)
        elif self.load_error is not None:
            self.pill.set_mode("text", f"❌ 蘋果辨識沒辦法啟動・按 {self.keyname} 重試")
        elif not self.ready:
            self.pill.set_mode("text", "準備語音辨識中…")
        elif self.pending:
            self.pill.set_mode("working")
        else:
            self.pill.set_mode("idle")

        if recording:
            self._set_icon("mic.fill", NSColor.systemRedColor())
        elif self.load_error is not None or self.notice:
            self._set_icon("exclamationmark.triangle", None)
        elif self.pending or not self.ready:
            self._set_icon("waveform", None)
        else:
            self._set_icon("mic", None)

    def _set_icon(self, symbol: str, tint) -> None:
        if self._icon == (symbol, tint):
            return
        self._icon = (symbol, tint)
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "語音輸入")
        image.setTemplate_(True)
        button = self.status_item.button()
        button.setImage_(image)
        button.setContentTintColor_(tint)

    def _flash(self, text: str, seconds: float) -> None:
        self.flash_text = text
        self.flash_until = time.monotonic() + seconds
        AppHelper.callLater(seconds + 0.05, self._refresh)
        self._refresh()

    def _play(self, name: str) -> None:
        if self.cfg["sounds"]:
            sound = NSSound.soundNamed_(name)
            if sound is not None:
                sound.setVolume_(0.4)
                sound.play()

    def _pill_clicked(self, view) -> None:
        if self.state != "idle":
            self._finish_recording()
        elif self.notice:
            open_url(SETTINGS_ACCESSIBILITY)
        else:
            self.menu.popUpMenuPositioningItem_atLocation_inView_(
                None, NSMakePoint(0, view.bounds().size.height + 6), view
            )

    def _pill_moved(self, anchor) -> None:
        self.cfg["pill_position"] = [round(anchor[0], 1), round(anchor[1], 1)]
        save_config(self.cfg)

    # ---- 設定選單（浮動條和選單列共用；每次打開時重新產生）

    def build_menu(self, menu) -> None:
        menu.removeAllItems()
        if self.state != "idle":
            status = "錄音中…"
        elif self.ready:
            status = f"待命中・按住 {self.keyname} 說話"
        else:
            status = "準備語音辨識中…"
        menu.addItem_(self._item(status))
        if self._fn_key_conflict():
            menu.addItem_(self._item("⚠️ 🌐 鍵會切換輸入法，點這裡去改設定…", lambda: open_url(SETTINGS_KEYBOARD)))
        if not Quartz.CGPreflightPostEventAccess():
            menu.addItem_(self._item("⚠️ 開啟「輔助使用」權限才能自動貼上…", lambda: open_url(SETTINGS_ACCESSIBILITY)))
        menu.addItem_(NSMenuItem.separatorItem())

        hotkeys = [(label, name) for name, (_, _, label) in HOTKEYS.items()]
        menu.addItem_(self._choice_menu("快捷鍵", hotkeys, self.cfg["hotkey"], self._set_hotkey))
        names = self.recorder.device_names(refresh=self.state == "idle")
        mics = [("系統預設", "")] + [(name, name) for name in names]
        if self.cfg["microphone"] and all(value != self.cfg["microphone"] for _, value in mics):
            mics.append((f"{self.cfg['microphone']}（未連接）", self.cfg["microphone"]))
        menu.addItem_(self._choice_menu("麥克風", mics, self.cfg["microphone"], self._set_microphone))
        menu.addItem_(self._item("自動更正…", self._edit_replacements))
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._item("提示音", self._toggle("sounds"), checked=self.cfg["sounds"]))
        menu.addItem_(self._item("平常也顯示浮動條", self._toggle("show_pill"), checked=self.cfg["show_pill"]))
        if APP_BUNDLE.exists():
            menu.addItem_(self._item("登入時自動啟動", self._toggle_login, checked=LAUNCH_AGENT.exists()))
        menu.addItem_(NSMenuItem.separatorItem())

        last = self._item("複製上一段文字", self._copy_last)
        last.setEnabled_(bool(self.last_text))
        menu.addItem_(last)
        menu.addItem_(self._item("開啟歷史紀錄", self._open_history))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._item("結束語音輸入", lambda: NSApplication.sharedApplication().terminate_(None)))

    def _item(self, title: str, callback=None, checked=None):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "fire:" if callback else None, ""
        )
        if callback:
            item.setTarget_(self.bridge)
            item.setRepresentedObject_(callback)
        else:
            item.setEnabled_(False)
        if checked is not None:
            item.setState_(NSControlStateValueOn if checked else NSControlStateValueOff)
        return item

    def _choice_menu(self, title: str, choices, current, setter):
        submenu = NSMenu.alloc().initWithTitle_(title)
        submenu.setAutoenablesItems_(False)
        for label, value in choices:
            submenu.addItem_(
                self._item(label, lambda value=value: setter(value), checked=value == current)
            )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setSubmenu_(submenu)
        return item

    def _set_setting(self, key: str):
        def setter(value):
            self.cfg[key] = value
            save_config(self.cfg)

        return setter

    def _toggle(self, key: str):
        def toggle():
            self.cfg[key] = not self.cfg[key]
            save_config(self.cfg)
            if key == "show_pill":
                self.pill.set_show_when_idle(self.cfg[key])

        return toggle

    def _set_hotkey(self, name: str) -> None:
        self._set_setting("hotkey")(name)
        self._apply_hotkey()
        self._flash(f"快捷鍵：{self.keyname}", 2)

    def _set_microphone(self, name: str) -> None:
        self._set_setting("microphone")(name)
        self.recorder.microphone = name
        self._flash(f"麥克風：{name or '系統預設'}", 2)

    def _edit_replacements(self) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("自動更正")
        alert.setInformativeText_(
            "每行一條：聽錯的字 → 正確的字\n"
            "例如「Cloud Call → Claude Code」。英文不分大小寫，空格有沒有都會抓到。"
        )
        alert.addButtonWithTitle_("儲存")
        alert.addButtonWithTitle_("取消")
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 380, 160))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        editor = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 380, 160))
        editor.setFont_(NSFont.systemFontOfSize_(13))
        editor.setRichText_(False)
        editor.setAutomaticQuoteSubstitutionEnabled_(False)
        editor.setString_(format_replacements(self.cfg["replacements"]) + "\n")
        scroll.setDocumentView_(editor)
        alert.setAccessoryView_(scroll)
        alert.window().setInitialFirstResponder_(editor)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if alert.runModal() == NSAlertFirstButtonReturn:
            rules = parse_replacements(editor.string())
            self._set_setting("replacements")(rules)
            self._flash(f"自動更正：{len(rules)} 條", 1.5)

    def _toggle_login(self) -> None:
        if LAUNCH_AGENT.exists():
            LAUNCH_AGENT.unlink()
            self._flash("已取消登入時自動啟動", 2)
            return
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        agent = {
            "Label": "local.voice-type",
            "ProgramArguments": ["/usr/bin/open", "-a", str(APP_BUNDLE)],
            "RunAtLoad": True,
        }
        with LAUNCH_AGENT.open("wb") as f:
            plistlib.dump(agent, f)
        self._flash("之後登入就會自動啟動", 2)

    def _copy_last(self) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self.last_text, NSPasteboardTypeString)
        self._flash("已複製上一段文字", 1.5)

    def _open_history(self) -> None:
        HISTORY_PATH.touch(exist_ok=True)
        subprocess.Popen(["open", "-t", str(HISTORY_PATH)])


def env_time(name: str) -> float:
    try:
        return float(os.environ.get(name, 0))
    except ValueError:
        return 0.0


def start_crash_guard() -> None:
    """從 App 打開時，另外開閃退監看（relaunch.py）：閃退的話自動重新打開 App。"""
    if os.environ.get("__CFBundleIdentifier") != APP_BUNDLE_ID or GUARD_ENV in os.environ:
        return  # 不是從 App 打開（例如在終端機測試），或原地重開過、已經有人在看著
    read_fd, write_fd = os.pipe()
    subprocess.Popen(
        [sys.executable, "-I", "-S", str(APP_DIR / "relaunch.py"), APP_BUNDLE_ID, str(os.getppid()),
         str(APP_DIR / "voice_type.log")],
        stdin=read_fd,
        start_new_session=True,
    )
    os.close(read_fd)
    os.set_inheritable(write_fd, True)  # 麥克風卡住原地重開（execv）後還要用
    os.environ[GUARD_ENV] = str(write_fd)


def tell_guard_quitting() -> None:
    """正常結束：告訴閃退監看不用重開。"""
    fd = os.environ.pop(GUARD_ENV, None)
    if fd is not None:
        try:
            os.write(int(fd), b"quit")
        except (OSError, ValueError):
            pass


def acquire_single_instance():
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return handle


def main() -> None:
    lock = acquire_single_instance()
    if lock is None:
        print("語音輸入已經在執行了。")
        sys.exit(0)
    cfg = load_config()
    print(f"語音輸入啟動中…（快捷鍵：{HOTKEYS[cfg['hotkey']][2]}）")
    if time.time() - env_time(CRASH_ENV) < 30:
        print("（剛剛閃退，已自動重新打開；閃退前的紀錄在 voice_type.crash.log）")
    start_crash_guard()

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    NSApplication.sharedApplication().setDelegate_(delegate)
    app = VoiceType(cfg)
    app.start()
    AppHelper.installMachInterrupt()
    MachSignals.signal(signal.SIGTERM, AppHelper.machInterrupt)  # 被要求結束（例如登出）也算正常結束
    AppHelper.runEventLoop()
    tell_guard_quitting()
    del lock


if __name__ == "__main__":
    main()
