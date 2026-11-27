#!/usr/bin/env python3
"""
核心能力：iPhone 镜像窗口定位 / 区域截图 / 广告判定 / 滚轮跳过

所有魔法数字和踩坑原因见 docs/技术实测数据.md
"""

import os
import re
import time
import threading
import logging

import AppKit
import Quartz
import numpy as np
from PIL import Image

try:
    import cv2
    _HAS_CV2 = True
except ImportError:          # 没有 cv2 就退回整区比对（位置一变就失效）
    _HAS_CV2 = False
import ApplicationServices as AS

# ---------------- 常量 ----------------

APP_NAME = "iPhone镜像"
APP_BUNDLE = "com.apple.ScreenContinuity"
MENUBAR_H = 30

# 「上滑继续看短剧」相对窗口 frame 的比例（实测标定）
TEXT_REL = {"x0": 0.37139, "x1": 0.62139, "y0": 0.92913, "y1": 0.94094}
CENTER_REL = {"x": 0.49639, "y": 0.93504}
# 监控区：比实测文字范围（x 0.37~0.62 / y 0.929~0.941）左右各留很大余量，
# 防止广告字位置漂移或字号变大被截断。配合模板「滑动搜索」，位置变也不影响判定。
MONITOR_REL = {"x0": 0.22, "x1": 0.78, "y0": 0.895, "y1": 0.975}

# 判定阈值
BRIGHT_MEAN_AD_MAX = 60.0   # 广告页监控区背景近黑（实测 15.1）
TEXT_PX_MIN = 300           # 文字像素下限（区域放大不影响文字像素总数）
NCC_THRESHOLD = 0.70        # 模板匹配阈值（实测 1.0）

# OCR 关键词：只要识别出这些片段就判定为广告页。
# 实测 Vision 会把左侧箭头 ⟨ 认成「公」，所以不能要求精确等于「上滑继续看短剧」。
AD_KEYWORDS_ANY = ("上滑", "继续看短剧", "滑继续看", "上滑继续")

# 倒计时锁定：广告会显示「5s后可继续上滑观看短剧」，倒计时结束前页面锁死，
# 这时候滑是滑不动的（会被顶回来，卡在中间态）。必须等它走完。
_COUNTDOWN_RE = re.compile(r"(\d{1,2})\s*(?:s|S|秒)\s*后")
COUNTDOWN_MAX = 15      # 超过这个秒数视为误识别（OCR 偶尔会把画面数字读进来）
OCR_MIN_INTERVAL = 0.3      # OCR 最短调用间隔
NCC_OCR_TRIGGER = 0.50      # 模板分 >= 此值才值得跑 OCR（播放画面实测 0.432）
OCR_FALLBACK_SEC = 20       # 完全陌生样式的兜底：每 N 秒强制 OCR 一次

# 滚轮（实测：必须用 CreateScrollWheelEvent2 + Pixel；sign=-1 才是上滑）
# 300px×4 档有时滑不到底（页面停在半路，文字上移出监控区→失明），
# 现改为首次 600×6，失败后按 SCROLL_ATTEMPTS 递增重试
SCROLL_PIXEL = 600
SCROLL_SIGN = -1
SCROLL_STEPS = 6
SCROLL_ATTEMPTS = [(600, 6), (900, 6), (1200, 8)]   # 依次尝试的 (像素, 步数)

# 动作时序（压缩后）；ACT_WAIT_FOCUS 是关键，太短会导致滚轮无效
ACT_WAIT_FOCUS = 0.25

# ---- 自适应监控频率 ----
SLOW_INTERVAL = 0.5    # 常态间隔（实测 CPU 0.6%，可忽略）
FAST_INTERVAL = 0.15   # 检测到画面变暗时的快扫间隔
DARK_DROP_RATIO = 0.25 # 亮度比上次下降超过这个比例 → 判定「可能进广告」
FAST_HOLD_TICKS = 15   # 快扫模式最多维持多少轮，之后回落到常态

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


# ---------------- OCR（macOS Vision，动态加载免安装） ----------------

_VISION = {"state": None}


def _load_vision():
    if _VISION["state"] is not None:
        return _VISION["state"] == "ok"
    try:
        import objc
        ns = {}
        objc.loadBundle("Vision", ns,
                        bundle_path="/System/Library/Frameworks/Vision.framework")
        req_cls = ns.get("VNRecognizeTextRequest")
        handler_cls = ns.get("VNImageRequestHandler")
        if req_cls is None or handler_cls is None:
            raise RuntimeError("Vision 符号不可见")
        _VISION.update(state="ok", req=req_cls, handler=handler_cls)
        log.info("Vision OCR 可用（免安装，动态加载）")
        return True
    except Exception as e:
        _VISION["state"] = "fail"
        log.warning("Vision OCR 不可用，仅靠模板匹配: %s", e)
        return False


def _to_cgimage(rgb):
    h, w = rgb.shape[:2]
    rgb = np.ascontiguousarray(rgb)
    provider = Quartz.CGDataProviderCreateWithData(None, rgb.tobytes(), rgb.nbytes, None)
    if provider is None:
        return None
    return Quartz.CGImageCreate(
        w, h, 8, 24, w * 3, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaNone | Quartz.kCGBitmapByteOrderDefault,
        provider, None, False, 0)


def ocr_texts(rgb):
    """返回识别到的文本片段列表。注：options 必须传 None，传 {} 会报 key does not exist。"""
    if not _load_vision():
        return []
    cg = _to_cgimage(rgb)
    if cg is None:
        return []
    try:
        req = _VISION["req"].alloc().init()
        req.setRecognitionLanguages_(["zh-Hans"])
        req.setRecognitionLevel_(0)          # 0 = fast，实测 23ms
        req.setUsesLanguageCorrection_(False)
        h = _VISION["handler"].alloc().initWithCGImage_options_(cg, None)
        h.performRequests_error_([req], None)
        out = []
        for r in (req.results() or []):
            cands = r.topCandidates_(1)
            if cands and len(cands):
                out.append(str(cands[0].string()))
        return out
    except Exception as e:
        log.warning("OCR 失败: %s", e)
        return []


def parse_countdown(texts):
    """
    从 OCR 文本里解析倒计时秒数。返回 (秒数 或 None, 原始片段)。
    例：「5s后可继续上滑观看短剧」→ (5, '5s后')
    """
    joined = "".join(texts)
    m = _COUNTDOWN_RE.search(joined)
    if not m:
        return None, ""
    try:
        sec = int(m.group(1))
    except ValueError:
        return None, m.group(0)
    if sec <= 0 or sec > COUNTDOWN_MAX:
        return None, m.group(0)
    return sec, m.group(0)


def ocr_looks_like_ad(texts):
    joined = "".join(texts)
    return any(k in joined for k in AD_KEYWORDS_ANY)


# ---------------- 判定 ----------------

_TEMPLATE_CACHE = {}


def _crop_to_text(tpl, pad=4):
    """裁到文字包围盒（留 pad 边距）。返回裁剪后的灰度数组。"""
    mask = (tpl > 95) & (tpl < 185)
    rows = np.where(mask.sum(axis=1) > 0)[0]
    cols = np.where(mask.sum(axis=0) > 0)[0]
    if not len(rows) or not len(cols):
        return tpl
    y0, y1 = max(0, rows[0] - pad), min(tpl.shape[0], rows[-1] + pad + 1)
    x0, x1 = max(0, cols[0] - pad), min(tpl.shape[1], cols[-1] + pad + 1)
    return tpl[y0:y1, x0:x1]


def load_templates():
    """
    加载 assets/template_*.png 全部模板。
    广告有多个样式（实测遇到过 2 种：「上滑继续看短剧 ⟨」和「⟨ 上滑继续看短剧」），
    必须多模板取最高分，否则换了样式的广告会漏检。
    """
    if "list" in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE["list"]
    import glob
    paths = sorted(glob.glob(os.path.join(ROOT, "assets", "template_*.png")))
    tpls = []
    for p in paths:
        try:
            g = np.array(Image.open(p).convert("L"), dtype=np.float32)
            # 关键：裁到文字包围盒。原模板 90% 是黑底，会让「黑底播放画面」也拿到高分
            # （实测裁剪前播放画面 0.455，裁剪后 0.432，但广告从 0.837 升到 1.000）
            g = _crop_to_text(g)
            tpls.append((os.path.basename(p), g))
        except Exception as e:
            log.warning("模板加载失败 %s: %s", p, e)
    _TEMPLATE_CACHE["list"] = tpls
    if tpls:
        log.info("已加载 %d 个模板: %s", len(tpls), [n for n, _ in tpls])
    else:
        log.warning("没有可用模板！请把广告截图存到 assets/template_*.png")
    return tpls


def load_template():
    """兼容旧接口：返回第一个模板。"""
    tpls = load_templates()
    return tpls[0][1] if tpls else None


def reload_templates():
    """清缓存重新加载（加新模板后不必重启）。"""
    _TEMPLATE_CACHE.clear()
    return load_templates()


def ncc(a, b):
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return 0.0 if denom < 1e-6 else float((a * b).sum() / denom)


def detect(gray, rgb=None, use_ocr=True):
    """
    三级判据。返回 (state, detail)，state ∈ {AD, PLAYING}

    ① 粗筛（微秒）：暗底 + 有中灰文字像素，否则直接 PLAYING
    ② 模板匹配（0.1ms）：任一模板 NCC 达标 → AD。适合已知样式，最快
    ③ OCR（23ms）：文字内容含关键词 → AD。免疫排版/字号/位置变化，用于新样式兜底
    """
    g = gray.astype(np.float32)
    mean = float(g.mean())
    text_px = int(((g > 100) & (g < 175)).sum())
    if mean > BRIGHT_MEAN_AD_MAX or text_px < TEXT_PX_MIN:
        return "PLAYING", {"mean": mean, "text_px": text_px}
    tpls = load_templates()
    best_score, best_name, best_loc = -1.0, None, None
    gh, gw = g.shape
    for name, tpl in tpls:
        t = tpl
        # 模板比监控区还大（用户把窗口缩得很小）→ 等比缩到区域的 70%
        if t.shape[0] > gh or t.shape[1] > gw:
            sc = min(gh / t.shape[0], gw / t.shape[1]) * 0.7
            t = np.array(Image.fromarray(t.astype(np.uint8)).resize(
                (max(8, int(t.shape[1] * sc)), max(8, int(t.shape[0] * sc))),
                Image.BILINEAR), dtype=np.float32)
        if _HAS_CV2:
            # 滑动搜索：文字位置/偏移怎么变都能找到（实测偏移 160px 仍 1.000 命中）
            res = cv2.matchTemplate(g, t.astype(np.float32), cv2.TM_CCOEFF_NORMED)
            _, mx, _, mloc = cv2.minMaxLoc(res)
            sc_val = float(mx)
            loc = mloc
        else:
            if t.shape != g.shape:
                t = np.array(Image.fromarray(t.astype(np.uint8)).resize(
                    (gw, gh), Image.BILINEAR), dtype=np.float32)
            sc_val, loc = ncc(g, t), (0, 0)
        if sc_val > best_score:
            best_score, best_name, best_loc = sc_val, name, loc

    if tpls and best_score >= NCC_THRESHOLD:
        d = {"mean": mean, "text_px": text_px, "ncc": round(best_score, 3),
             "tpl": best_name, "by": "模板", "loc": best_loc}
        # 模板命中也要确认有没有倒计时锁定（否则会在锁定期间白滑，卡在半路）
        if use_ocr and rgb is not None:
            texts = ocr_texts(rgb)
            cd, cd_raw = parse_countdown(texts)
            if cd is not None:
                d["countdown"] = cd
                d["countdown_raw"] = cd_raw
                d["ocr"] = "".join(texts)[:30]
        return "AD", d

    # 模板不达标：只有「有点像」（>= NCC_OCR_TRIGGER）才值得花 33ms 跑 OCR。
    # 播放画面实测最高 0.432，所以常态不会触发 OCR。
    if use_ocr and rgb is not None and best_score >= NCC_OCR_TRIGGER:
        texts = ocr_texts(rgb)
        if ocr_looks_like_ad(texts):
            cd, cd_raw = parse_countdown(texts)
            d = {"mean": mean, "text_px": text_px, "ncc": round(best_score, 3),
                 "tpl": best_name, "by": "OCR", "ocr": "".join(texts)[:30]}
            if cd is not None:
                d["countdown"] = cd
                d["countdown_raw"] = cd_raw
            return "AD", d
        return "PLAYING", {"mean": mean, "text_px": text_px, "ncc": round(best_score, 3),
                           "tpl": best_name, "by": "OCR未命中",
                           "ocr": "".join(texts)[:30]}

    return "PLAYING", {"mean": mean, "text_px": text_px, "ncc": round(best_score, 3),
                       "tpl": best_name, "by": "粗筛"}


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
        time.sleep(0.01)
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


def skip_ad_at(px, py, pixel=None, steps=None):
    """
    完整跳过：移入 → 点击激活 → 滚轮上滚 → 鼠标复位。

    各 sleep 已压到能生效的下限（原序列 1.74s → 现 0.69s）。
    若发现偶发失效，优先调大 ACT_WAIT_FOCUS（点击后等 iOS 接管的时长）。
    """
    orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    _move_mouse(px, py, steps=3)
    time.sleep(0.12)
    _click_at(px, py)
    time.sleep(ACT_WAIT_FOCUS)
    _move_mouse(px, py, steps=3)
    time.sleep(0.08)
    _scroll_px(SCROLL_SIGN * (pixel if pixel else SCROLL_PIXEL),
               steps=steps if steps else SCROLL_STEPS)
    Quartz.CGWarpMouseCursorPosition((orig.x, orig.y))


def ad_text_visible_anywhere(frame):
    """
    在窗口下部 45% 范围里找「上滑继续看短剧」。
    用于滚动后验证：滚动过程中文字会整体上移，固定监控区可能已经看不到它，
    但页面其实还停在广告页。这里用 OCR 在整个下半部找，不受位置限制。
    """
    x, y, w, h = frame
    band = grab_region(x, y + 0.55 * h, w, 0.45 * h)
    if band is None:
        return None
    texts = ocr_texts(band)
    return ocr_looks_like_ad(texts)


# ---------------- 监控器 ----------------

class Monitor:
    """
    后台监控线程。回调签名：
      on_event(kind, info)  kind ∈ {started, stopped, ad_skipped, disconnected, error, tick}
      on_status(state)      state ∈ {idle, watching, ad, disconnected, error}
    """

    def __init__(self, interval=None, on_event=None, on_status=None, shots_dir=None,
                 slow_interval=SLOW_INTERVAL, fast_interval=FAST_INTERVAL):
        # interval 兼容旧接口：传了就当常态间隔
        self.slow_interval = interval if interval else slow_interval
        self.fast_interval = fast_interval
        self.interval = self.slow_interval
        self.shots_dir = shots_dir
        self.max_shots = 200
        if shots_dir:
            os.makedirs(shots_dir, exist_ok=True)
        self.on_event = on_event or (lambda *a: None)
        self.on_status = on_status or (lambda *a: None)
        self._stop = threading.Event()
        self._thread = None
        self.stats = {"checks": 0, "ads": 0, "skipped": 0, "failed": 0,
                      "disconnected": 0}
        self.last_error = ""
        self._last_conn_check = 0.0
        self._last_frame = None
        self._shot_seq = 0
        self._prev_mean = None
        self._fast_left = 0
        self._last_interval = None
        self._last_near_miss = 0.0
        self._last_fallback_ocr = 0.0
        self._last_countdown = None

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

    def _check_near_miss(self, frame, detail):
        """
        粗筛通过、但模板和 OCR 都没认定 → 存证 + 警告（60 秒一次）。
        意味着既不是已知样式，OCR 也没读出广告关键词，需要人工看一眼。
        """
        if detail.get("by") != "OCR未命中":
            return
        if (detail.get("text_px", 0) < TEXT_PX_MIN
                or detail.get("mean", 999) >= BRIGHT_MEAN_AD_MAX):
            return
        now = time.time()
        if now - self._last_near_miss < 60:
            return
        self._last_near_miss = now
        path = None
        if self.shots_dir:
            arr, _ = snapshot(frame)
            path = self.save_shot(arr, "UNKNOWN_AD_STYLE")
        log.warning("疑似新广告样式（模板未匹配 ncc=%s, mean=%.1f, text_px=%d）存证=%s"
                    " → 请把这张图加到 assets/template_ad_textN.png",
                    detail.get("ncc"), detail.get("mean", 0), detail.get("text_px", 0),
                    os.path.basename(path) if path else "无")
        self.on_event("unknown_style", {"detail": detail,
                                        "shot": os.path.basename(path) if path else None})

    # ---- 自适应频率 ----
    def _update_frequency(self, mean):
        """
        画面突然变暗（广告开始）→ 切快扫，最快捕捉到「上滑继续看短剧」出现的那一刻。
        短剧画面本身的明暗变化会误触发，但代价只是短暂提高采样率，无害。
        """
        if mean is None:
            return
        if self._prev_mean and self._prev_mean > 1:
            drop = (self._prev_mean - mean) / self._prev_mean
            if drop > DARK_DROP_RATIO:
                if self._fast_left <= 0:
                    log.debug("亮度骤降 %.0f→%.0f (%.0f%%) → 快扫模式",
                              self._prev_mean, mean, drop * 100)
                self._fast_left = FAST_HOLD_TICKS
        if self._fast_left > 0:
            self._fast_left -= 1
        self._prev_mean = mean

    def _current_interval(self):
        iv = self.fast_interval if self._fast_left > 0 else self.slow_interval
        if iv != self._last_interval:
            log.debug("采样间隔 %.2fs → %.2fs", self._last_interval or 0, iv)
            self._last_interval = iv
        return iv

    # ---- 存证（出问题时唯一能回看的证据）----
    def save_shot(self, arr, tag, jpeg_quality=0, scale=1.0):
        """
        把截图存进 shots_dir，带时间戳和标签；超过上限自动删最旧的。

        jpeg_quality > 0 时存 JPEG（整窗大图必须用，否则单张近 900KB）
        scale < 1.0 时先缩小
        """
        if not self.shots_dir or arr is None:
            return None
        try:
            self._shot_seq += 1
            img = Image.fromarray(arr)
            if scale != 1.0:
                img = img.resize((max(1, int(img.width * scale)),
                                  max(1, int(img.height * scale))), Image.LANCZOS)
            if jpeg_quality:
                ext = "jpg"
                path = os.path.join(
                    self.shots_dir,
                    f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}_{self._shot_seq:03d}.{ext}")
                img.save(path, "JPEG", quality=jpeg_quality, optimize=True)
            else:
                path = os.path.join(
                    self.shots_dir,
                    f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}_{self._shot_seq:03d}.png")
                img.save(path)
            files = sorted(
                (os.path.join(self.shots_dir, f) for f in os.listdir(self.shots_dir)
                 if f.endswith((".png", ".jpg"))),
                key=os.path.getmtime)
            for old in files[:-self.max_shots]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            return path
        except Exception as e:
            log.warning("存证失败: %s", e)
            return None

    def snapshot_env(self, frame):
        """环境快照，用于复现问题。"""
        screen = AppKit.NSScreen.mainScreen().frame()
        tpl = load_template()
        return {
            "screen": f"{screen.size.width:.0f}x{screen.size.height:.0f}",
            "window": f"({frame[0]:.0f},{frame[1]:.0f}) {frame[2]:.0f}x{frame[3]:.0f}",
            "ratio": round(frame[2] / frame[3], 5),
            "monitor_box": tuple(round(v, 1) for v in monitor_box(frame)),
            "template": (f"{tpl.shape[1]}x{tpl.shape[0]}" if tpl is not None else "无"),
            "thresholds": f"mean<{BRIGHT_MEAN_AD_MAX} text_px>={TEXT_PX_MIN} ncc>={NCC_THRESHOLD}",
            "scroll": f"sign={SCROLL_SIGN} pixel={SCROLL_PIXEL} steps={SCROLL_STEPS}",
            "interval": f"slow={self.slow_interval}s fast={self.fast_interval}s "
                        f"drop>{DARK_DROP_RATIO}",
        }

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
                    self._fast_left = 0
                self._stop.wait(self._current_interval())
        finally:
            log.info("监控结束 stats=%s", self.stats)
            self.on_event("stopped", dict(self.stats))

    def _tick(self):
        self.stats["checks"] += 1
        log.debug("tick #%d", self.stats["checks"])
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

        # 窗口位置/尺寸变了 → 坐标会算错，必须留痕
        if self._last_frame != frame:
            if self._last_frame is None:
                log.info("环境快照 %s", self.snapshot_env(frame))
            else:
                log.warning("窗口变化 %s → %s  (环境: %s)", self._last_frame, frame,
                            self.snapshot_env(frame))
            self._last_frame = frame

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

        rgb, gray = snapshot(frame)
        if gray is None:
            self.on_event("error", {"msg": "监控区截图失败"})
            return
        state, detail = detect(gray, rgb)
        self._update_frequency(detail.get("mean"))

        if state != "AD":
            # 兜底：连"有点像"都不到时，也会定期强制 OCR 一次，
            # 防止出现和已知样式完全不相似的第 N 种广告被永久漏掉
            if (detail.get("by") == "粗筛"
                    and time.time() - self._last_fallback_ocr > OCR_FALLBACK_SEC):
                self._last_fallback_ocr = time.time()
                texts = ocr_texts(rgb) if rgb is not None else []
                if ocr_looks_like_ad(texts):
                    log.info("兜底 OCR 命中: %s", "".join(texts)[:30])
                    state = "AD"
                    detail = dict(detail, by="OCR兜底", ocr="".join(texts)[:30])
            if state != "AD":
                self._check_near_miss(frame, detail)
            self.on_status("watching")
            return

        # ---- 命中广告 ----
        cd = detail.get("countdown")
        if cd:
            # 倒计时锁定：这时候滑不动（会被顶回来卡在半路），只能等
            if cd != self._last_countdown:
                log.info("倒计时 %ds 中（%s）→ 页面锁定，等待解锁，不动作",
                         cd, detail.get("countdown_raw", ""))
                self._last_countdown = cd
            self._fast_left = FAST_HOLD_TICKS   # 解锁瞬间要立刻抓住，保持快扫
            self.on_status("ad_wait")
            self.on_event("ad_countdown", {"sec": cd, "ocr": detail.get("ocr", "")})
            return
        self._last_countdown = None

        self.stats["ads"] += 1
        self.on_status("ad")
        cx, cy = center_point(frame)
        shot = self.save_shot(
            snapshot(frame)[0], f"adhit_ncc{detail.get('ncc')}") if self.shots_dir else None
        log.info("AD 命中 %s → 跳过 (%.0f, %.0f) 存证=%s", detail, cx, cy,
                 os.path.basename(shot) if shot else "无")
        self.on_event("ad_hit", dict(detail, x=cx, y=cy))

        # 滚动可能滑不到底（页面停半路），失败就加大力度重试
        for attempt, (px, steps) in enumerate(SCROLL_ATTEMPTS, 1):
            skip_ad_at(cx, cy, pixel=px, steps=steps)
            self._stop.wait(0.9)
            _, frame_now = get_window()
            if frame_now is None:
                break
            still = ad_text_visible_anywhere(frame_now)
            log.info("第 %d 次滚动 (%dpx×%d) → 广告文字仍可见=%s", attempt, px, steps, still)
            if not still:
                break
            if self._stop.is_set():
                return
        frame = frame_now or frame

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
            fail_shot = None
            if self.shots_dir:
                full_win = grab_region(x, y, w, h)
                fail_shot = self.save_shot(full_win, "ADFAIL_fullwindow",
                                           jpeg_quality=75, scale=0.75)
            log.warning("跳过可能失败 → %s 画面亮度=%.1f 存证=%s", state2, full_mean,
                        os.path.basename(fail_shot) if fail_shot else "无")
            self.on_event("ad_failed", {"state": state2, "full_mean": round(full_mean, 1)})
        self._fast_left = 0
        self._stop.wait(0.8)
