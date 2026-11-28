#!/usr/bin/env python3
"""
iPhone 镜像 · 短剧去广告 —— 菜单栏 app

跑法：
  ~/Projects/mcp/.venv/bin/python src/app.py

菜单栏图标含义：
  ⏸  已停止
  ▶︎  监控中（等待广告）
  ⚡︎  正在跳过广告
  ⚠︎  镜像未连接
  ✕  出错（悬停看原因）
"""

import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import AppKit
from AppKit import (NSApplication, NSStatusBar, NSVariableStatusItemLength,
                    NSMenu, NSMenuItem, NSObject, NSApplicationActivationPolicyAccessory)
from PyObjCTools import AppHelper
import objc

import core

VERSION = "2.0"          # 改这一行即可；菜单、tooltip 与启动日志都会跟着显示

ROOT = core.ROOT
LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "ad_monitor.log")
CONTROL_FILE = os.path.join(ROOT, ".control")   # 写 start / stop 即可控制，无需点菜单

from logging.handlers import RotatingFileHandler

SHOTS_DIR = os.path.join(ROOT, "logs", "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s")

# 主日志：轮转，5MB × 5 份
_main = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                            backupCount=5, encoding="utf-8")
_main.setFormatter(_fmt)
_main.setLevel(logging.DEBUG)

# 错误单独一份，排查时只看它
_err = RotatingFileHandler(os.path.join(LOG_DIR, "errors.log"),
                           maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_err.setFormatter(_fmt)
_err.setLevel(logging.WARNING)

_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_console.setLevel(logging.INFO)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
# 第三方库噪音太大，只留警告
for noisy in ("PIL", "urllib3", "Quartz"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
for h in (_main, _err, _console):
    root_logger.addHandler(h)
log = logging.getLogger("app")

# 图标按「点一下会执行什么」的媒体按钮语义：跑着时显示 ⏸（点了就暂停），
# 停下时显示 ▶︎（点了就开跑）。其它状态是事件指示，显示当时正在干的事。
ICONS = {
    "idle": "▶︎",         # 已停止 → 点图标 = 开始监控
    "watching": "⏸",      # 监控中 → 点图标 = 暂停
    "ad": "⚡︎",
    "ad_wait": "⏳",      # 倒计时锁定中，等解锁
    "ad_skipped": "⚡︎",
    "product": "⬅︎",      # 误进商品详情页，正在点左上角返回
    "disconnected": "⚠︎",
    "error": "✕",
}

STATUS_TEXT = {
    "idle": "已停止（点图标开始监控）",
    "watching": "监控中（点图标暂停）",
    "ad": "正在跳过广告",
    "ad_wait": "广告倒计时中，等解锁",
    "product": "误进商品页，点左上角返回中",
    "disconnected": "镜像未连接",
    "error": "出错",
}


class AppDelegate(NSObject):

    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.status_item = None
        self.monitor = None
        self.state = "idle"
        self.last_msg = ""
        self.pin_window = True
        self.auto_start = True
        return self

    # ---------- UI 构建 ----------

    def buildMenu(self):
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self.status_item.button()
        btn.setTitle_(ICONS["idle"])
        btn.setToolTip_(f"短剧去广告 v{VERSION}\n左键点图标 = 开始 / 暂停，右键点 = 菜单")
        btn.setTarget_(self)
        btn.setAction_("iconClicked:")     # 默认只在「左键抬起」时触发
        # ⚠️ PyObjC 没暴露 NSStatusBarButton 的 setSendActionOn_（button 本身与它的 cell
        # 都没有，调一下就报 AttributeError 并静默中断 buildMenu，图标停在初始值）
        # → 右键改用全局事件监控抓「右键按下」，落在本图标范围内才弹菜单。
        self._right_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskRightMouseDown, lambda ev: self._maybe_popup_menu(ev))
        self._build_menu_items()
        AppHelper.callLater(1.5, self._log_icon_pos)

    @objc.python_method
    def _log_icon_pos(self):
        """把图标在屏幕上的位置写进日志，方便定位/点按。"""
        try:
            win = self.status_item.button().window()
            f = win.frame()
            log.info("菜单栏图标 屏幕逻辑坐标 x=%.0f~%.0f y=%.0f~%.0f  中心(%.0f,%.0f)",
                     f.origin.x, f.origin.x + f.size.width,
                     f.origin.y, f.origin.y + f.size.height,
                     f.origin.x + f.size.width / 2,
                     AppKit.NSScreen.mainScreen().frame().size.height
                     - (f.origin.y + f.size.height / 2))
        except Exception as e:
            log.warning("取图标位置失败: %s", e)

    def _build_menu_items(self):
        menu = NSMenu.alloc().init()

        self.item_status = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "状态：已停止", "", "")
        self.item_status.setEnabled_(False)
        menu.addItem_(self.item_status)

        self.item_stats = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "跳广告 0 次 / 检查 0 次", "", "")
        self.item_stats.setEnabled_(False)
        menu.addItem_(self.item_stats)

        menu.addItem_(NSMenuItem.separatorItem())

        self.item_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "开始监控", "toggleMonitor:", "")
        self.item_toggle.setTarget_(self)
        menu.addItem_(self.item_toggle)

        self.item_pin = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "启动时把镜像窗口固定到左上角", "togglePin:", "")
        self.item_pin.setTarget_(self)
        self.item_pin.setState_(1)
        menu.addItem_(self.item_pin)

        self.item_auto = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "启动时自动监控", "toggleAuto:", "")
        self.item_auto.setTarget_(self)
        self.item_auto.setState_(1)
        menu.addItem_(self.item_auto)

        self.item_test = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "检测一次（不动作）", "testOnce:", "")
        self.item_test.setTarget_(self)
        menu.addItem_(self.item_test)

        menu.addItem_(NSMenuItem.separatorItem())

        item_log = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开日志", "openLog:", "")
        item_log.setTarget_(self)
        menu.addItem_(item_log)

        item_shots = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开存证截图文件夹", "openShots:", "")
        item_shots.setTarget_(self)
        menu.addItem_(item_shots)

        item_ver = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"版本 v{VERSION}", "", "")
        item_ver.setEnabled_(False)
        menu.addItem_(item_ver)

        item_quit = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出", "quitApp:", "q")
        item_quit.setTarget_(self)
        menu.addItem_(item_quit)

        self.menu = menu
        # 不再 setMenu_(menu)：那样左键也会弹菜单，图标就当不了「开始/暂停」按钮。
        # 左键走 iconClicked_（直接切换监控），右键时手动 popUp 这个 menu。

    # ---------- 状态刷新 ----------

    def refreshUI(self):
        st = self.state
        running = bool(self.monitor and self.monitor.running)
        icon = ICONS.get(st)
        if icon is None:                       # 未知状态：按在不在跑给 暂停/播放
            icon = "⏸" if running else "▶︎"
        elif running and st in ("idle", "error"):
            icon = "⏸"                        # 在跑但状态滞后 → 仍是「可暂停」
        elif not running and st not in ("idle", "error"):
            icon = "▶︎"                        # 已停但状态未刷 → 仍是「可开始」
        self.status_item.button().setTitle_(icon)
        log.debug("图标 → %s（state=%s running=%s）", icon, st, running)
        tip = STATUS_TEXT.get(st, st)
        if self.last_msg:
            tip += "\n" + self.last_msg
        self.status_item.button().setToolTip_(tip)

        self.item_status.setTitle_("状态：" + STATUS_TEXT.get(st, st))
        self.item_toggle.setTitle_("停止监控" if self.monitor and self.monitor.running
                                   else "开始监控")
        if self.monitor:
            s = self.monitor.stats
            self.item_stats.setTitle_(
                f"跳广告 {s['skipped']} 次（失败 {s['failed']}）/ 检查 {s['checks']} 次")
        else:
            self.item_stats.setTitle_("跳广告 0 次 / 检查 0 次")

    # ---------- 监控回调（后台线程 → 主线程） ----------

    @objc.python_method
    def onStatus(self, state):
        if state != self.state:
            log.info("状态 %s → %s", self.state, state)
        def apply():
            self.state = state
            self.refreshUI()
        AppHelper.callAfter(apply)

    @objc.python_method
    def onEvent(self, kind, info):
        def apply():
            if kind == "ad_countdown":
                self.last_msg = f"倒计时 {info.get('sec')}s，等解锁后再滑"
            elif kind == "ad_hit":
                self.last_msg = f"命中广告 ncc={info.get('ncc')}"
            elif kind == "ad_skipped":
                self.last_msg = f"已跳过，画面亮度 {info.get('full_mean')}"
            elif kind == "ad_failed":
                self.last_msg = f"跳过可能失败（{info.get('state')}）"
            elif kind == "disconnected":
                self.last_msg = "iPhone 镜像已断开，请锁定 iPhone 后点「连接」"
            elif kind == "error":
                self.last_msg = info.get("msg", "")
            elif kind == "stopped":
                self.last_msg = "已停止"
            self.refreshUI()
        AppHelper.callAfter(apply)

    # ---------- 菜单动作 ----------

    def iconClicked_(self, sender):
        """图标本身的点击：左键 = 开始/暂停切换，右键（或 Ctrl+左键）= 弹菜单。"""
        ev = NSApp.currentEvent()
        is_right = (ev is not None
                    and (ev.type() in (AppKit.NSEventTypeRightMouseDown,
                                       AppKit.NSEventTypeRightMouseUp)
                         or (ev.modifierFlags() & AppKit.NSEventModifierFlagControl)))
        if not is_right:
            log.info("点图标 → 切换监控（当前 running=%s）",
                     bool(self.monitor and self.monitor.running))
            self.toggleMonitor_(sender)
            return
        self._maybe_popup_menu(ev)

    @objc.python_method
    def _maybe_popup_menu(self, ev=None):
        """弹状态菜单：右键由全局事件监控触发（需确认真的点在本图标上），
        Ctrl+左键由 iconClicked_ 转发过来。"""
        try:
            win = self.status_item.button().window()
            if win is None:
                return
            f = win.frame()
            m = AppKit.NSEvent.mouseLocation()          # 全局屏幕坐标（原点左下角）
            if not (f.origin.x <= m.x <= f.origin.x + f.size.width
                    and f.origin.y <= m.y <= f.origin.y + f.size.height):
                return                                  # 右键点在别处，与本图标无关
            log.info("右键/Ctrl+左键点在图标上 → 弹出菜单")
            self.menu.popUpMenuPositioningItem_atLocation_inView_(
                None, AppKit.NSMakePoint(0, 0), win.contentView())
        except Exception as e:
            log.warning("弹出菜单失败：%s", e)

    def toggleMonitor_(self, sender):
        if self.monitor and self.monitor.running:
            self.stop_monitor()
        else:
            self.start_monitor()

    @objc.python_method
    def stop_monitor(self):
        if self.monitor and self.monitor.running:
            log.info("停止监控")
            self.monitor.stop()
        self.state = "idle"
        self.last_msg = "已停止"
        self.refreshUI()

    @objc.python_method
    def start_monitor(self):
        if self.monitor and self.monitor.running:
            return

        # 前置检查：镜像在跑吗
        app = core.find_app()
        if app is None:
            self.state = "error"
            self.last_msg = "请先打开「iPhone 镜像」app"
            self.refreshUI()
            return

        axw, frame = core.get_window(app)
        if frame is None:
            self.state = "error"
            self.last_msg = "读不到镜像窗口"
            self.refreshUI()
            return

        if self.pin_window and axw is not None:
            core.pin_window(axw)
            axw, frame = core.get_window(app)

        ok, info = core.check_connected(frame)
        if not ok:
            self.state = "disconnected"
            self.last_msg = f"镜像未连接 {info}"
            self.refreshUI()
            return

        self.monitor = core.Monitor(on_event=self.onEvent,
                                    on_status=self.onStatus,
                                    shots_dir=SHOTS_DIR)
        self.monitor.start()
        self.state = "watching"
        self.last_msg = f"窗口 ({frame[0]:.0f},{frame[1]:.0f}) {frame[2]:.0f}x{frame[3]:.0f}"
        self.refreshUI()

    def toggleAuto_(self, sender):
        self.auto_start = not self.auto_start
        self.item_auto.setState_(1 if self.auto_start else 0)

    def togglePin_(self, sender):
        self.pin_window = not self.pin_window
        self.item_pin.setState_(1 if self.pin_window else 0)

    def testOnce_(self, sender):
        app = core.find_app()
        if app is None:
            self.state = "error"
            self.last_msg = "请先打开「iPhone 镜像」app"
            self.refreshUI()
            return
        axw, frame = core.get_window(app)
        if frame is None:
            self.state = "error"
            self.last_msg = "读不到镜像窗口"
            self.refreshUI()
            return
        _, gray = core.snapshot(frame)
        if gray is None:
            self.state = "error"
            self.last_msg = "截图失败"
            self.refreshUI()
            return
        state, detail = core.detect(gray)
        conn_ok, conn_info = core.check_connected(frame)
        self.state = "idle" if state != "AD" else "ad"
        self.last_msg = (f"判定 {state} mean={detail['mean']:.1f} "
                         f"ncc={detail.get('ncc')} 连接={conn_ok} {conn_info}")
        log.info("手动检测 → %s %s conn=%s", state, detail, conn_ok)
        self.refreshUI()

    def openLog_(self, sender):
        NSApp = NSApplication.sharedApplication()
        NSApp.activateIgnoringOtherApps_(True)
        AppKit.NSWorkspace.sharedWorkspace().openFile_(LOG_PATH)

    def openShots_(self, sender):
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        AppKit.NSWorkspace.sharedWorkspace().openFile_(SHOTS_DIR)

    def quitApp_(self, sender):
        if self.monitor and self.monitor.running:
            self.monitor.stop()
        NSApplication.sharedApplication().terminate_(None)

    # ---------- 启动 ----------

    @objc.python_method
    def _control_loop(self):
        """轮询控制文件：echo start > .control 即可启动监控，echo stop > .control 停止。"""
        import threading
        def run():
            while True:
                try:
                    if os.path.exists(CONTROL_FILE):
                        cmd = open(CONTROL_FILE, encoding="utf-8").read().strip().lower()
                        os.remove(CONTROL_FILE)
                        log.info("收到控制命令: %s", cmd)
                        if cmd in ("start", "开始"):
                            AppHelper.callAfter(self.start_monitor)
                        elif cmd in ("stop", "停止"):
                            AppHelper.callAfter(self.stop_monitor)
                        elif cmd in ("status", "状态"):
                            log.info("状态=%s stats=%s", self.state,
                                     self.monitor.stats if self.monitor else None)
                except Exception as e:
                    log.warning("控制文件处理失败: %s", e)
                time.sleep(0.4)
        threading.Thread(target=run, name="control", daemon=True).start()

    @objc.python_method
    def _auto_start(self):
        """启动后自动开始监控（可用菜单「启动时自动监控」关掉）。"""
        if not self.auto_start:
            log.info("自动监控已关闭，等待手动启动")
            return
        log.info("自动启动监控")
        self.start_monitor()

    @objc.python_method
    def _warmup(self):
        """后台预热 OCR（首次调用要加载 Vision 模型，约 1 秒，不能让它卡在第一支广告上）。"""
        import threading
        def run():
            try:
                import numpy as np
                dummy = np.zeros((60, 218, 3), dtype=np.uint8)
                t0 = time.time()
                core.ocr_texts(dummy)
                log.info("OCR 预热完成 %.0fms", (time.time() - t0) * 1000)
            except Exception as e:
                log.warning("OCR 预热失败: %s", e)
        threading.Thread(target=run, daemon=True).start()

    def applicationDidFinishLaunching_(self, notification):
        log.info("iPhone 镜像 · 红果短剧去广告 v%s 启动（pid=%d）", VERSION, os.getpid())
        self.buildMenu()
        self.refreshUI()
        self._warmup()
        self._control_loop()
        AppHelper.callLater(2.0, self._auto_start)
        app_w, frame = core.get_window(core.find_app())
        env = self.monitor.snapshot_env(frame) if (self.monitor and frame) else None
        if frame:
            m = core.Monitor(shots_dir=SHOTS_DIR)
            log.info("启动 环境=%s", m.snapshot_env(frame))
        else:
            log.info("启动（镜像未运行）")
        log.info("日志=%s  存证=%s  错误日志=%s", LOG_PATH, SHOTS_DIR,
                 os.path.join(LOG_DIR, "errors.log"))


def main():
    app = NSApplication.sharedApplication()
    # 纯菜单栏，不占 Dock
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
