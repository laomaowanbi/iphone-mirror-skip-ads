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

ROOT = core.ROOT
LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "ad_monitor.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("app")

ICONS = {
    "idle": "⏸",
    "watching": "▶︎",
    "ad": "⚡︎",
    "ad_skipped": "⚡︎",
    "disconnected": "⚠︎",
    "error": "✕",
}

STATUS_TEXT = {
    "idle": "已停止",
    "watching": "监控中",
    "ad": "正在跳过广告",
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
        return self

    # ---------- UI 构建 ----------

    def buildMenu(self):
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.button().setTitle_(ICONS["idle"])
        self.status_item.button().setToolTip_("短剧去广告")
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

        self.item_test = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "检测一次（不动作）", "testOnce:", "")
        self.item_test.setTarget_(self)
        menu.addItem_(self.item_test)

        menu.addItem_(NSMenuItem.separatorItem())

        item_log = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开日志", "openLog:", "")
        item_log.setTarget_(self)
        menu.addItem_(item_log)

        item_quit = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出", "quitApp:", "q")
        item_quit.setTarget_(self)
        menu.addItem_(item_quit)

        self.status_item.setMenu_(menu)

    # ---------- 状态刷新 ----------

    def refreshUI(self):
        st = self.state
        self.status_item.button().setTitle_(ICONS.get(st, "⏸"))
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
        def apply():
            self.state = state
            self.refreshUI()
        AppHelper.callAfter(apply)

    @objc.python_method
    def onEvent(self, kind, info):
        def apply():
            if kind == "ad_hit":
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

    def toggleMonitor_(self, sender):
        if self.monitor and self.monitor.running:
            log.info("用户停止监控")
            self.monitor.stop()
            self.state = "idle"
            self.last_msg = "已手动停止"
            self.refreshUI()
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

        self.monitor = core.Monitor(interval=3.0,
                                    on_event=self.onEvent,
                                    on_status=self.onStatus)
        self.monitor.start()
        self.state = "watching"
        self.last_msg = f"窗口 ({frame[0]:.0f},{frame[1]:.0f}) {frame[2]:.0f}x{frame[3]:.0f}"
        self.refreshUI()

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

    def quitApp_(self, sender):
        if self.monitor and self.monitor.running:
            self.monitor.stop()
        NSApplication.sharedApplication().terminate_(None)

    # ---------- 启动 ----------

    def applicationDidFinishLaunching_(self, notification):
        self.buildMenu()
        self.refreshUI()
        log.info("菜单栏 app 启动，日志 %s", LOG_PATH)


def main():
    app = NSApplication.sharedApplication()
    # 纯菜单栏，不占 Dock
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
