#!/bin/bash
# 停止菜单栏 app
pkill -f "src/app.py" && echo "已停止" || echo "没在运行"
