#!/bin/bash
# 启动菜单栏 app（前台运行，Ctrl+C 退出；关终端请用 nohup 见下）
#   后台常驻：nohup ./run.sh >/tmp/iphone_ad_app.log 2>&1 &
cd "$(dirname "$0")" || exit 1
exec ~/Projects/mcp/.venv/bin/python src/app.py "$@"
