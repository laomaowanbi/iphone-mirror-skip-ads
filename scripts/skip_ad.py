#!/usr/bin/env python3
"""
M1 核心能力验证：检测广告 → 滚轮跳过

用法：
  ~/Projects/mcp/.venv/bin/python scripts/skip_ad.py --dry-run      # 只检测，不动鼠标
  ~/Projects/mcp/.venv/bin/python scripts/skip_ad.py --once         # 检测一次，命中就滚
  ~/Projects/mcp/.venv/bin/python scripts/skip_ad.py --probe-dir    # 试两个滚轮方向，各滚一次看哪个有效

实测数据来源见 docs/技术实测数据.md
"""

import os
import sys
import time
import argparse

import AppKit
import Quartz
import numpy as np
from PIL import Image
import ApplicationServices as AS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

APP_NAME = "iPhone镜像"
MENUBAR_H = 30

MONITOR_REL = {"x0": 0.34, "x1": 0.66, "y0": 0.915, "y1": 0.955}
CENTER_REL = {"x": 0.49639, "y": 0.93504}
TEMPLATE_PATH = os.path.join(ROOT, "assets", "template_ad_text.png")

# 判定阈值（用当前实测样本标定）
BRIGHT_MEAN_AD_MAX = 60     # 广告页背景近黑
TEXT_PX_MIN = 300           # 文字像素下限
NCC_THRESHOLD = 0.70        # 模板匹配阈值

# ---- 滚轮参数（实测标定，见 docs/技术实测数据.md 第 9 节）----
# 必须用 CGEventCreateScrollWheelEvent2（变参版在 pyobjc 下无效！）
# 单位必须 Pixel（Line 单位无效）
# 方向：用户实测确认 SIGN = -1 才是「上滑」。（我曾用 +1 也让广告消失，
#       但可能两个方向都能滑走广告、只是 -1 才是滑向下一集的正解。）
SCROLL_PIXEL = 300
SCROLL_SIGN = -1
SCROLL_STEPS = 4


# ---------------- 窗口 ----------------

def get_window_frame():
    app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                if (a.localizedName() or "") == APP_NAME), None)
    if app is None:
        return None
    axapp = AS.AXUIElementCreateApplication(app.processIdentifier())
    err, wins = AS.AXUIElementCopyAttributeValue(axapp, AS.kAXWindowsAttribute, None)
    if err != 0 or not wins:
        return None
    w = wins[0]
    e1, posv = AS.AXUIElementCopyAttributeValue(w, AS.kAXPositionAttribute, None)
    e2, sizev = AS.AXUIElementCopyAttributeValue(w, AS.kAXSizeAttribute, None)
    if e1 or e2:
        return None
    okp, pos = AS.AXValueGetValue(posv, AS.kAXValueCGPointType, None)
    oks, size = AS.AXValueGetValue(sizev, AS.kAXValueCGSizeType, None)
    if not (okp and oks):
        return None
    return (pos.x, pos.y, size.width, size.height)


# ---------------- 截图 ----------------

def grab_region(x, y, w, h):
    rect = Quartz.CGRectMake(float(int(x)), float(int(y)), float(int(w)), float(int(h)))
    cg = Quartz.CGDisplayCreateImageForRect(Quartz.CGMainDisplayID(), rect)
    if cg is None:
        return None
    cw, ch = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    bpr = Quartz.CGImageGetBytesPerRow(cg)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
    return np.frombuffer(data, dtype=np.uint8)[: ch * bpr].reshape(ch, bpr // 4, 4)[:, :cw, :3]


# ---------------- 判定 ----------------

def ncc(a, b):
    """归一化互相关，a/b 为同尺寸灰度 float 数组。"""
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    if denom < 1e-6:
        return 0.0
    return float((a * b).sum() / denom)


_TEMPLATE_CACHE = {}


def load_template():
    if "g" in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE["g"]
    if not os.path.exists(TEMPLATE_PATH):
        return None
    im = Image.open(TEMPLATE_PATH).convert("L")
    g = np.array(im, dtype=np.float32)
    _TEMPLATE_CACHE["g"] = g
    return g


def detect(frame_gray):
    """
    双层判据。返回 (state, detail)
      state: 'AD' / 'PLAYING' / 'DISCONNECTED'
    """
    g = frame_gray.astype(np.float32)
    mean = float(g.mean())
    text_px = int(((g > 100) & (g < 175)).sum())

    # 断开页：偏亮的灰色大块 + 几乎没有中灰文字行
    if mean > 150:
        return "DISCONNECTED", {"mean": mean, "text_px": text_px}

    if mean > BRIGHT_MEAN_AD_MAX or text_px < TEXT_PX_MIN:
        return "PLAYING", {"mean": mean, "text_px": text_px}

    # 粗筛通过 → 模板匹配
    tpl = load_template()
    if tpl is None:
        return "PLAYING", {"mean": mean, "text_px": text_px, "note": "无模板"}
    if tpl.shape != g.shape:
        tpl = np.array(Image.fromarray(tpl.astype(np.uint8)).resize(
            (g.shape[1], g.shape[0]), Image.BILINEAR), dtype=np.float32)
    score = ncc(g, tpl)
    state = "AD" if score >= NCC_THRESHOLD else "PLAYING"
    return state, {"mean": mean, "text_px": text_px, "ncc": round(score, 3)}


# ---------------- 动作 ----------------

def move_mouse(px, py, steps=5):
    """分步移动鼠标，产生 mouseMoved 序列，让窗口感知「鼠标进入」。"""
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


def click_at(px, py):
    """点击一次。实测必需：iPhone 镜像窗口需要点击后滚轮事件才会被转发给 iOS。"""
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    pt = Quartz.CGPointMake(px, py)
    for typ in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(src, typ, pt, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.06)


def scroll_px(delta, steps=SCROLL_STEPS):
    """像素单位滚轮。必须用 CGEventCreateScrollWheelEvent2。"""
    for _ in range(steps):
        ev = Quartz.CGEventCreateScrollWheelEvent2(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(delta), 0, 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def skip_ad_at(x, y):
    """完整跳过动作：移入 → 点击（激活）→ 滚轮上滚 → 鼠标复位。"""
    orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    move_mouse(x, y)
    time.sleep(0.25)
    click_at(x, y)          # 关键：不点这一下，滚轮事件不会被 iOS 接收
    time.sleep(0.5)
    move_mouse(x, y)
    time.sleep(0.15)
    scroll_px(SCROLL_SIGN * SCROLL_PIXEL)
    time.sleep(0.35)
    Quartz.CGWarpMouseCursorPosition((orig.x, orig.y))
    return orig


# ---------------- 主流程 ----------------

def snapshot(frame):
    x, y, w, h = frame
    mx0 = x + MONITOR_REL["x0"] * w
    mx1 = x + MONITOR_REL["x1"] * w
    my0 = y + MONITOR_REL["y0"] * h
    my1 = y + MONITOR_REL["y1"] * h
    arr = grab_region(mx0, my0, mx1 - mx0, my1 - my0)
    if arr is None:
        return None, None
    return arr, arr.mean(axis=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只检测，不动作")
    ap.add_argument("--once", action="store_true", help="检测一次，命中就滚")
    ap.add_argument("--probe-dir", action="store_true", help="两个方向各试一次，测滚轮方向")
    ap.add_argument("--save", default="", help="保存本次监控区截图")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="监控模式：持续跑 SEC 秒，遇到广告就自动跳过")
    args = ap.parse_args()

    frame = get_window_frame()
    if frame is None:
        print("❌ iPhone 镜像未运行或读不到窗口")
        return 2
    x, y, w, h = frame
    print(f"窗口 pos=({x},{y}) size=({w},{h})")

    # 监控模式优先，不受下面的单次检测逻辑影响
    if args.watch:
        return watch_loop(frame, args.watch)

    arr, gray = snapshot(frame)
    if arr is None:
        print("❌ 截图失败")
        return 3
    if args.save:
        Image.fromarray(arr).save(args.save)

    t0 = time.perf_counter()
    state, detail = detect(gray)
    print(f"判定 {state}  {detail}   ({(time.perf_counter()-t0)*1000:.1f}ms)")

    if args.dry_run or state != "AD":
        return 0

    cx = x + CENTER_REL["x"] * w
    cy = y + CENTER_REL["y"] * h

    if args.once:
        print(f"跳过动作于 ({cx:.0f},{cy:.0f}) sign={SCROLL_SIGN} pixel={SCROLL_PIXEL}")
        skip_ad_at(cx, cy)
        time.sleep(1.2)
        arr2, g2 = snapshot(frame)
        st2, d2 = detect(g2)
        print(f"滚动后判定 {st2}  {d2}")
        return 0 if st2 != "AD" else 1

    return 0


def watch_loop(frame, seconds):
    """持续监控，遇广告就跳过。日志打印到 stdout。"""
    x, y, w, h = frame
    cx = x + CENTER_REL["x"] * w
    cy = y + CENTER_REL["y"] * h
    t_end = time.time() + seconds
    n_check = n_skip = n_fail = 0
    print(f"监控 {seconds}s  目标点 ({cx:.0f},{cy:.0f})  sign={SCROLL_SIGN} pixel={SCROLL_PIXEL}")
    print("-" * 60, flush=True)
    while time.time() < t_end:
        n_check += 1
        arr, gray = snapshot(frame)
        if gray is None:
            print(f"[{n_check}] 截图失败")
            time.sleep(3)
            continue
        state, detail = detect(gray)
        ts = time.strftime("%H:%M:%S")
        if state != "AD":
            print(f"[{ts}] #{n_check} {state} mean={detail['mean']:.1f}", flush=True)
            time.sleep(3)
            continue

        print(f"[{ts}] #{n_check} 🎯 AD 命中  mean={detail['mean']:.1f} ncc={detail.get('ncc')} → 执行跳过", flush=True)
        skip_ad_at(cx, cy)
        time.sleep(1.2)

        arr2, g2 = snapshot(frame)
        st2, d2 = detect(g2)
        # 交叉验证：整窗是否恢复成有画面的正片（不仅监控区无文字）
        full = None
        try:
            full = grab_region(x, y + h * 0.15, w, h * 0.55)
        except Exception:
            pass
        full_mean = float(full.mean()) if full is not None else -1
        ok = (st2 != "AD") and full_mean > 25
        if ok:
            n_skip += 1
            print(f"         ✅ 跳过成功 → {st2}  画面亮度={full_mean:.1f}", flush=True)
        else:
            n_fail += 1
            print(f"         ❌ 可能失败 → {st2}  画面亮度={full_mean:.1f}", flush=True)
        time.sleep(2.5)

    print("-" * 60)
    print(f"结束：检查 {n_check} 次，命中广告 {n_skip + n_fail} 次，成功 {n_skip}，失败 {n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
