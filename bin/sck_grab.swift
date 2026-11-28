// sck_grab —— iPhone 镜像画面常驻采集器
// 用 ScreenCaptureKit 的 SCStream(录屏流采集语义,**不是截图 API**) → 实测不会让 iPhone 产生截图事件
// 输出:
//   /tmp/frame.raw   RGB24 连续字节(宽×高×3),原子替换(.tmp → rename)
//   /tmp/frame.meta  "w h ts_ms"(窗口消失时 w=0 h=0)
// 由 Python 端 core.py 读取,替代 CGDisplayCreateImageForRect
import ScreenCaptureKit
import CoreMedia
import Foundation
import AppKit
import CoreVideo

_ = NSApplication.shared
NSApplication.shared.setActivationPolicy(.accessory)   // 不占 Dock

let RAW = "/tmp/frame.raw", RAWT = "/tmp/frame.raw.tmp"
let META = "/tmp/frame.meta", METAT = "/tmp/frame.meta.tmp"
let LOGPATH = "/Users/nantiange/projects/iphone镜像去广告/logs/sck_grab.log"

func log(_ s: String) {
    let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd HH:mm:ss,SSS"
    let line = "\(df.string(from: Date())) \(s)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
    if let h = FileHandle(forWritingAtPath: LOGPATH) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile()
    } else { try? line.write(toFile: LOGPATH, atomically: false, encoding: .utf8) }
}

func writeMeta(_ w: Int, _ h: Int) {
    let m = "\(w) \(h) \(Int(Date().timeIntervalSince1970 * 1000))\n"
    do {
        try m.write(toFile: METAT, atomically: false, encoding: .utf8)
        if FileManager.default.fileExists(atPath: META) { try FileManager.default.removeItem(atPath: META) }
        try FileManager.default.moveItem(atPath: METAT, toPath: META)
    } catch { log("meta 写入失败 \(error)") }
}

final class Collector: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    var windowID = 0
    var frames = 0
    var scale = 2.0

    // BGRA → RGB24 落盘
    func stream(_ s: SCStream, didOutputSampleBuffer b: CMSampleBuffer, of t: SCStreamOutputType) {
        guard t == .screen else { return }
        guard let img = CMSampleBufferGetImageBuffer(b) else { return }
        CVPixelBufferLockBaseAddress(img, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(img, .readOnly) }
        let w = CVPixelBufferGetWidth(img), h = CVPixelBufferGetHeight(img)
        let bpr = CVPixelBufferGetBytesPerRow(img)
        guard let base = CVPixelBufferGetBaseAddress(img) else { return }
        let src = base.assumingMemoryBound(to: UInt8.self)
        var rgb = [UInt8](repeating: 0, count: w * h * 3)
        rgb.withUnsafeMutableBytes { dst in
            guard let d = dst.bindMemory(to: UInt8.self).baseAddress else { return }
            for y in 0..<h {
                let row = src + y * bpr
                let drow = d + y * w * 3
                for x in 0..<w {
                    let p = row + x * 4, q = drow + x * 3
                    q[0] = p[2]; q[1] = p[1]; q[2] = p[0]      // B G R A → R G B
                }
            }
        }
        let data = Data(bytes: rgb, count: rgb.count)
        do {
            try data.write(to: URL(fileURLWithPath: RAWT))
            if FileManager.default.fileExists(atPath: RAW) { try FileManager.default.removeItem(atPath: RAW) }
            try FileManager.default.moveItem(at: URL(fileURLWithPath: RAWT), to: URL(fileURLWithPath: RAW))
            writeMeta(w, h)
            frames += 1
            if frames % 200 == 1 { log("帧 #\(frames) \(w)x\(h)") }
        } catch { log("写帧失败 \(error.localizedDescription)") }
    }

    func stream(_ s: SCStream, didStopWithError e: Error) {
        log("采集中断: \(e.localizedDescription)")
        windowID = 0
    }
}

let col = Collector()

func pickWindow(_ c: SCShareableContent) -> SCWindow? {
    c.windows.first { ($0.owningApplication?.applicationName == "iPhone镜像")
        && $0.frame.width > 300 && $0.frame.height > 600 }
}

func rebuild(_ win: SCWindow) async {
    if let old = col.stream {
        do { try await old.stopCapture() } catch { log("停止旧采集失败 \(error)") }
        col.stream = nil
    }
    let cfg = SCStreamConfiguration()
    let k = col.scale                       // 按 retina 倍率输出 → 与原 CG 截图同为物理像素,阈值无需改
    cfg.width = Int(win.frame.width * k)
    cfg.height = Int(win.frame.height * k)
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 2)   // 2fps = 0.5s,与脚本同频
    cfg.queueDepth = 3
    cfg.showsCursor = false
    cfg.pixelFormat = kCVPixelFormatType_32BGRA
    let st = SCStream(filter: SCContentFilter(desktopIndependentWindow: win),
                      configuration: cfg, delegate: col)
    do {
        try st.addStreamOutput(col, type: .screen,
                               sampleHandlerQueue: DispatchQueue.global(qos: .userInteractive))
        try await st.startCapture()
        col.stream = st
        col.windowID = Int(win.windowID)
        log("开始采集 id=\(win.windowID) \(cfg.width)x\(cfg.height) @2fps")
    } catch {
        log("启动采集失败: \(error.localizedDescription)")
        col.windowID = 0
    }
}

func refresh() async {
    do {
        let content = try await SCShareableContent.current
        guard let win = pickWindow(content) else {
            if col.windowID != 0 {
                log("镜像窗口消失 → 停止采集")
                if let st = col.stream { try? await st.stopCapture(); col.stream = nil }
                col.windowID = 0
                writeMeta(0, 0)
            }
            return
        }
        if Int(win.windowID) != col.windowID || col.stream == nil {
            log("窗口新建/变化 id=\(win.windowID) frame=\(win.frame)")
            await rebuild(win)
        }
    } catch { log("枚举共享内容失败 \(error.localizedDescription)") }
}

col.scale = Double(NSScreen.main?.backingScaleFactor ?? 2.0)
log("采集器启动 scale=\(col.scale)")

Task { await refresh() }
Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in Task { await refresh() } }
RunLoop.main.run()
