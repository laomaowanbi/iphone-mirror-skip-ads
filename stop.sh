#!/bin/bash
# 停止菜单栏 app（pkill + 清理 launcher 用的 pidfile）
DIR="$(cd "$(dirname "$0")" && pwd)"
if pkill -f "src/app\.py"; then echo "已停止监控"; else echo "监控没在运行"; fi
rm -f "$DIR/logs/app.pid"
if pkill -x sck_grab; then echo "✅ SCK 采集器已停"; else echo "采集器没在运行"; fi
