#!/bin/bash
# 启动菜单栏 app（前台运行，Ctrl+C 退出；后台常驻请用 App 双击或 nohup ./run.sh）
#   后台常驻：nohup ./run.sh >/tmp/iphone_ad_app.log 2>&1 &
cd "$(dirname "$0")" || exit 1
mkdir -p logs
# 先起 SCK 采集器：用 ScreenCaptureKit 取镜像窗口画面，不会在 iPhone 端产生截图事件
# （否则回退 CG 截图 → 红果每 0.5 秒弹一次「识图找同款」）
if ! pgrep -x sck_grab >/dev/null 2>&1; then
    if [ -x bin/sck_grab ]; then
        : > logs/sck_grab.log
        nohup ./bin/sck_grab >/dev/null 2>&1 &
        echo "✅ SCK 采集器已启动 pid=$!"
        sleep 1
    else
        echo "⚠️ 缺 bin/sck_grab → 回退 CG 截图（会弹识图浮窗）。编译：\
              swiftc -O -o bin/sck_grab bin/sck_grab.swift"
    fi
fi
exec ~/Projects/mcp/.venv/bin/python src/app.py "$@"
