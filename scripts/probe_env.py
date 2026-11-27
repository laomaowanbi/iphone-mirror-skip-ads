#!/usr/bin/env python3
"""
环境与窗口探测（只读，可反复运行，不改任何东西）

用途：
  1. 确认 iPhone 镜像 app 是否在运行
  2. 读取窗口 frame，算出「上滑继续看短剧」的屏幕坐标
  3. 检查镜像是连着还是断开
  4. 自检权限与可用 API

跑法：
  ~/Projects/mcp/.venv/bin/python scripts/probe_env.py
  ~/Projects/mcp/.venv/bin/python scripts/probe_env.py --move     # 顺带把窗口移到左上角
"""

import sys
import time
import argparse

import AppKit
import Quartz
import numpy as np
from PIL import Image
import ApplicationServices as AS

# ---------------- 常量（来自实测，见 docs/技术实测数据.md） ----------------

APP_NAME = "iPhone镜像"
APP_BUNDLE = "com.apple.ScreenContinuity"

# 「上滑继续看短剧」相对窗口 frame 的比例
TEXT_REL = {"x0": 0.37139, "x1": 0.62139, "y0": 0.92913, "y1": 0.94094}
CENTER_REL = {"x": 0.49639, "y": 0.93504}
# 监控区（留边距）
MONITOR_REL = {"x0": 0.34, "x1": 0.66, "y0": 0.915, "y1": 0.955}

MENUBAR_H = 30  # 菜单栏高度（pt）
TARGET_POS = (0, MENUBAR_H)


# ---------------- 基础工具 ----------------

def get_ax_window():
    """返回 (app, ax_window, frame)，app 未运行返回 None。"""
    app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                if (a.localizedName() or "") == APP_NAME), None)
    if app is None:
        return None
    pid = app.processIdentifier()
    axapp = AS.AXUIElementCreateApplication(pid)
    err, wins = AS.AXUIElementCopyAttributeValue(axapp, AS.kAXWindowsAttribute, None)
    if err != 0 or not wins:
        return app, None, None
    w = wins[0]

    def get(attr):
        e, v = AS.AXUIElementCopyAttributeValue(w, attr, None)
        return v if e == 0 else None

    posv, sizev = get(AS.kAXPositionAttribute), get(AS.kAXSizeAttribute)
    if posv is None or sizev is None:
        return app, w, None
    okp, pos = AS.AXValueGetValue(posv, AS.kAXValueCGPointType, None)
    oks, size = AS.AXValueGetValue(sizev, AS.kAXValueCGSizeType, None)
    if not (okp and oks):
        return app, w, None
    return app, w, (pos.x, pos.y, size.width, size.height)


def move_window_to_target(ax_window):
    """把窗口移到左上角。返回 (err, 实际frame)。"""
    val = AS.AXValueCreate(AS.kAXValueCGPointType, (float(TARGET_POS[0]), float(TARGET_POS[1])))
    err = AS.AXUIElementSetAttributeValue(ax_window, AS.kAXPositionAttribute, val)
    time.sleep(0.3)
    return err


def grab_region(x, y, w, h):
    """用 CGDisplayCreateImageForRect 抓屏幕区域，返回 numpy RGB 数组（物理像素）。"""
    rect = Quartz.CGRectMake(float(x), float(y), float(w), float(h))
    cg = Quartz.CGDisplayCreateImageForRect(Quartz.CGMainDisplayID(), rect)
    if cg is None:
        return None
    cw, ch = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    bpr = Quartz.CGImageGetBytesPerRow(cg)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
    arr = np.frombuffer(data, dtype=np.uint8)[: ch * bpr].reshape(ch, bpr // 4, 4)[:, :cw, :3]
    return arr


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--move", action="store_true", help="把窗口移到左上角 (0,%d)" % MENUBAR_H)
    ap.add_argument("--save", default="", help="把监控区截图存到这个路径，便于肉眼核对")
    args = ap.parse_args()

    print("=" * 60)
    print("① 权限自检")
    print("   辅助功能 AXIsProcessTrusted =", AS.AXIsProcessTrusted())

    print("=" * 60)
    print("② iPhone 镜像 app 检测")
    got = get_ax_window()
    if got is None:
        print("   ❌ 未运行。请先打开「iPhone 镜像」app。")
        return 2
    app, axw, frame = got
    print(f"   ✅ 运行中  bundle={app.bundleIdentifier()}  pid={app.processIdentifier()}")
    if frame is None:
        print("   ❌ 读不到窗口 frame（可能窗口未就绪）")
        return 3
    x, y, w, h = frame
    print(f"   窗口 frame: pos=({x}, {y})  size=({w}, {h})  比例={w/h:.5f}")

    print("=" * 60)
    print("③ 窗口位置")
    if args.move:
        err = move_window_to_target(axw)
        got = get_ax_window()
        frame = got[2]
        x, y, w, h = frame
        print(f"   已尝试移动 err={err} → pos=({x}, {y}) size=({w}, {h})")
    else:
        print(f"   当前 pos=({x}, {y})（加 --move 可移到左上角）")

    print("=" * 60)
    print("④ 目标坐标换算")
    cx = x + CENTER_REL["x"] * w
    cy = y + CENTER_REL["y"] * h
    print(f"   「上滑继续看短剧」中心（屏幕逻辑坐标）≈ ({cx:.1f}, {cy:.1f})")

    mx0, mx1 = x + MONITOR_REL["x0"] * w, x + MONITOR_REL["x1"] * w
    my0, my1 = y + MONITOR_REL["y0"] * h, y + MONITOR_REL["y1"] * h
    print(f"   监控区（屏幕逻辑坐标）: ({mx0:.0f}, {my0:.0f}) 起，"
          f"{mx1-mx0:.0f}×{my1-my0:.0f}")

    print("=" * 60)
    print("⑤ 抓取监控区样本")
    t0 = time.perf_counter()
    arr = grab_region(int(mx0), int(my0), int(mx1 - mx0), int(my1 - my0))
    dt = (time.perf_counter() - t0) * 1000
    if arr is None:
        print("   ❌ 抓取失败（屏幕录制权限？）")
        return 4
    mean = arr.mean()
    gray = arr.mean(axis=2)
    text_px = int(((gray > 100) & (gray < 175)).sum())
    print(f"   ✅ {dt:.1f}ms  形状={arr.shape}")
    print(f"   亮度均值={mean:.1f}   疑似文字像素={text_px}")
    verdict = "AD（疑似广告页）" if (mean < 60 and text_px > 80) else "PLAYING / 无广告"
    print(f"   粗筛结论: {verdict}")
    if mean > 200:
        print("   ⚠️ 整区过亮，可能是窗口被遮挡或镜像已断开")

    if args.save:
        Image.fromarray(arr).save(args.save)
        print(f"   已存 {args.save}")

    print("=" * 60)
    print("完成。注意：粗筛只是省时间，真正判定要用模板匹配。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
