#!/usr/bin/env python3
"""
核心能力：iPhone 镜像窗口定位 / 区域截图 / 广告判定 / 滚轮跳过

所有魔法数字和踩坑原因见 docs/技术实测数据.md
"""

import os
import time
import threading
import logging

import AppKit
import Quartz
import numpy as np
from PIL import Image
import ApplicationServices as AS

# ---------------- 常量 ----------------

APP_NAME = "iPhone镜像"
APP_BUNDLE = "com.apple.ScreenContinuity"
MENUBAR_H = 30

# 「上滑继续看短剧」相对窗口 frame 的比例（实测标定）
TEXT_REL = {"x0": 0.37139, "x1": 0.62139, "y0": 0.92913, "y1": 0.94094}
CENTER_REL = {"x": 0.49639, "y": 0.93504}
MONITOR_REL = {"x0": 0.34, "x1": 0.66, "y0": 0.915, "y1": 0.955}

# 判定阈值
BRIGHT_MEAN_AD_MAX = 60.0   # 广告页监控区背景近黑（实测 15.1）
TEXT_PX_MIN = 300           # 文字像素下限（实测 1083）
NCC_THRESHOLD = 0.70        # 模板匹配阈值（实测 1.0）

# 滚轮（实测：必须用 CreateScrollWheelEvent2 + Pixel；sign=-1 才是上滑）
SCROLL_PIXEL = 300
SCROLL_SIGN = -1
SCROLL_STEPS = 4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE_PATH = os.path.join(ROOT, "assets", "template_ad_text.png")

log = logging.getLogger("core")


# ---------------- 窗口 ----------------

def find_app():
    return next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                 if (a.localizedName() or "") == APP_NAME), None)


def get_window(app=None):
    """返回 (ax_window, (x, y, w, h))；失败返回 (None, None)。"""
    app = app or find_app()
    if app is None:
        return None, None
    axapp = AS.AXUIElementCreateApplication(app.processIdentifier())
    err, wins = AS.AXUIElementCopyAttributeValue(axapp, AS.kAXWindowsAttribute, None)
    if err != 0 or not wins:
        return None, None
    w = wins[0]

    def get(attr):
        e, v = AS.AXUIElementCopyAttributeValue(w, attr, None)
        return v if e == 0 else None

    posv, sizev = get(AS.kAXPositionAttribute), get(AS.kAXSizeAttribute)
    if posv is None or sizev is None:
        return w, None
    okp, pos = AS.AXValueGetValue(posv, AS.kAXValueCGPointType, None)
    oks, size = AS.AXValueGetValue(sizev, AS.kAXValueCGSizeType, None)
    if not (okp and oks):
        return w, None
    return w, (pos.x, pos.y, size.width, size.height)


def pin_window(ax_window, pos=(0, MENUBAR_H)):
    """把窗口移到固定位置。注意：绝不设 Size（系统会重算）。"""
    val = AS.AXValueCreate(AS.kAXValueCGPointType, (float(pos[0]), float(pos[1])))
    err = AS.AXUIElementSetAttributeValue(ax_window, AS.kAXPositionAttribute, val)
    time.sleep(0.3)
    return err == 0


# ---------------- 截图 ----------------

def grab_region(x, y, w, h):
    """抓屏幕区域，返回 RGB numpy 数组（物理像素）。"""
    if w < 1 or h < 1:
        return None
    rect = Quartz.CGRectMake(float(int(x)), float(int(y)), float(int(w)), float(int(h)))
    cg = Quartz.CGDisplayCreateImageForRect(Quartz.CGMainDisplayID(), rect)
    if cg is None:
        return None
    cw, ch = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    bpr = Quartz.CGImageGetBytesPerRow(cg)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
    return np.frombuffer(data, dtype=np.uint8)[: ch * bpr].reshape(
        ch, bpr // 4, 4)[:, :cw, :3]


def monitor_box(frame):
    x, y, w, h = frame
    return (x + MONITOR_REL["x0"] * w,
            y + MONITOR_REL["y0"] * h,
            (MONITOR_REL["x1"] - MONITOR_REL["x0"]) * w,
            (MONITOR_REL["y1"] - MONITOR_REL["y0"]) * h)


def center_point(frame):
    x, y, w, h = frame
    return (x + CENTER_REL["x"] * w, y + CENTER_REL["y"] * h)


def snapshot(frame):
    """抓监控区，返回 (rgb, gray)。"""
    bx, by, bw, bh = monitor_box(frame)
    arr = grab_region(bx, by, bw, bh)
    if arr is None:
        return None, None
    return arr, arr.mean(axis=2)


# ---------------- 判定 ----------------

_TEMPLATE_CACHE = {}


def load_template():
    if "g" in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE["g"]
    if not os.path.exists(TEMPLATE_PATH):
        return None
    g = np.array(Image.open(TEMPLATE_PATH).convert("L"), dtype=np.float32)
    _TEMPLATE_CACHE["g"] = g
    return g


def ncc(a, b):
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return 0.0 if denom < 1e-6 else float((a * b).sum() / denom)


def detect(gray):
    """双层判据。返回 (state, detail)，state ∈ {AD, PLAYING}。"""
    g = gray.astype(np.float32)
    mean = float(g.mean())
    text_px = int(((g > 100) & (g < 175)).sum())
    if mean > BRIGHT_MEAN_AD_MAX or text_px < TEXT_PX_MIN:
        return "PLAYING", {"mean": mean, "text_px": text_px}
    tpl = load_template()
    if tpl is None:
        return "PLAYING", {"mean": mean, "text_px": text_px, "note": "无模板"}
    if tpl.shape != g.shape:
        tpl = np.array(Image.fromarray(tpl.astype(np.uint8)).resize(
            (g.shape[1], g.shape[0]), Image.BILINEAR), dtype=np.float32)
    score = ncc(g, tpl)
    return ("AD" if score >= NCC_THRESHOLD else "PLAYING",
            {"mean": mean, "text_px": text_px, "ncc": round(score, 3)})


def check_connected(frame):
    """
    检查镜像是否连着。断开页是纯灰 UI（饱和度极低），播放页有彩色画面。
    返回 (ok, info)
    """
    x, y, w, h = frame
    # 取窗口中部（视频区）
    arr = grab_region(x + w * 0.1, y + h * 0.25, w * 0.8, h * 0.45)
    if arr is None:
        return False, {"reason": "截图失败"}
    a = arr.astype(np.int16)
    sat = float(np.abs(a[:, :, 0] - a[:, :, 2]).mean())  # 红蓝通道差 = 彩色度
    mean = float(a.mean())
    ok = sat > 6.0
    return ok, {"sat": round(sat, 1), "mean": round(mean, 1)}


# ---------------- 鼠标动作 ----------------

def _move_mouse(px, py, steps=5):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    start = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    for i in range(1, steps + 1):
        ix = start.x + (px - start.x) * i / steps
        iy = start.y + (py - start.y) * i / steps
        ev = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved,
                                            Quartz.CGPointMake(ix, iy), 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.02)
    Quartz.CGWarpMouseCursorPosition((px, py))


def _click_at(px, py):
    """实测必需：iPhone 镜像窗口点击过后滚轮事件才会转发给 iOS。"""
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    pt = Quartz.CGPointMake(px, py)
    for typ in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(src, typ, pt, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.06)


def _scroll_px(delta, steps=SCROLL_STEPS):
    """实测：必须 CGEventCreateScrollWheelEvent2，变参版在 pyobjc 下无效。"""
    for _ in range(steps):
        ev = Quartz.CGEventCreateScrollWheelEvent2(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(delta), 0, 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def skip_ad_at(px, py):
    """完整跳过：移入 → 点击激活 → 滚轮上滚 → 鼠标复位。"""
    orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    _move_mouse(px, py)
    time.sleep(0.25)
    _click_at(px, py)
    time.sleep(0.5)
    _move_mouse(px, py)
    time.sleep(0.15)
    _scroll_px(SCROLL_SIGN * SCROLL_PIXEL)
    time.sleep(0.35)
    Quartz.CGWarpMouseCursorPosition((orig.x, orig.y))


# ---------------- 监控器 ----------------

class Monitor:
    """
    后台监控线程。回调签名：
      on_event(kind, info)  kind ∈ {started, stopped, ad_skipped, disconnected, error, tick}
      on_status(state)      state ∈ {idle, watching, ad, disconnected, error}
    """

    def __init__(self, interval=3.0, on_event=None, on_status=None):
        self.interval = interval
        self.on_event = on_event or (lambda *a: None)
        self.on_status = on_status or (lambda *a: None)
        self._stop = threading.Event()
        self._thread = None
        self.stats = {"checks": 0, "ads": 0, "skipped": 0, "failed": 0,
                      "disconnected": 0}
        self.last_error = ""
        self._last_conn_check = 0.0

    # ---- 生命周期 ----
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ad-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        self._thread = None
        self.on_status("idle")

    # ---- 主循环 ----
    def _loop(self):
        self.on_event("started", {})
        log.info("监控启动 interval=%.1fs", self.interval)
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception as e:      # 单次异常不能杀死监控
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.exception("tick 异常")
                    self.on_event("error", {"msg": self.last_error})
                    self.on_status("error")
                self._stop.wait(self.interval)
        finally:
            log.info("监控结束 stats=%s", self.stats)
            self.on_event("stopped", dict(self.stats))

    def _tick(self):
        self.stats["checks"] += 1
        app = find_app()
        if app is None:
            self.on_status("error")
            self.on_event("error", {"msg": "iPhone 镜像 app 未运行"})
            self._stop.wait(5)
            return

        _, frame = get_window(app)
        if frame is None:
            self.on_status("error")
            self.on_event("error", {"msg": "读不到镜像窗口"})
            self._stop.wait(5)
            return

        # 每 15 秒检查一次镜像是否断开（截图有成本）
        now = time.time()
        if now - self._last_conn_check > 15:
            self._last_conn_check = now
            ok, info = check_connected(frame)
            if not ok:
                self.stats["disconnected"] += 1
                self.on_status("disconnected")
                self.on_event("disconnected", info)
                return

        _, gray = snapshot(frame)
        if gray is None:
            self.on_event("error", {"msg": "监控区截图失败"})
            return
        state, detail = detect(gray)

        if state != "AD":
            self.on_status("watching")
            return

        # ---- 命中广告 ----
        self.stats["ads"] += 1
        self.on_status("ad")
        cx, cy = center_point(frame)
        log.info("AD 命中 %s → 跳过 (% .0f, % .0f)", detail, cx, cy)
        self.on_event("ad_hit", dict(detail, x=cx, y=cy))
        skip_ad_at(cx, cy)
        self._stop.wait(1.2)

        # ---- 验证：不仅监控区无文字，整窗也要恢复成有画面的正片 ----
        _, gray2 = snapshot(frame)
        state2 = detect(gray2)[0] if gray2 is not None else "AD"
        x, y, w, h = frame
        full = grab_region(x, y + h * 0.15, w, h * 0.55)
        full_mean = float(full.mean()) if full is not None else -1
        if state2 != "AD" and full_mean > 25:
            self.stats["skipped"] += 1
            log.info("跳过成功 → %s 画面亮度=%.1f", state2, full_mean)
            self.on_event("ad_skipped", {"state": state2, "full_mean": round(full_mean, 1)})
            self.on_status("watching")
        else:
            self.stats["failed"] += 1
            log.warning("跳过可能失败 → %s 画面亮度=%.1f", state2, full_mean)
            self.on_event("ad_failed", {"state": state2, "full_mean": round(full_mean, 1)})
        self._stop.wait(2.5)
