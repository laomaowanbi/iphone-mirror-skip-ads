#!/bin/bash
# iPhone镜像去广告 —— 菜单栏监控启动器（双击启动 / 防双开）
#   单实例判定用 pidfile + kill -0，不用 pgrep -f（pgrep 会误命中任何命令行含
#   "src/app.py" 的进程，例如 grep、编辑器、别的 shell，导致误报"已在运行"）
DIR="$HOME/projects/iphone镜像去广告"
PY="$HOME/Projects/mcp/.venv/bin/python"
PIDFILE="$DIR/logs/app.pid"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

if running; then
	osascript -e 'display notification "监控程序已在运行（PID '"$(cat "$PIDFILE")"'）—— 点菜单栏 ▶︎/⏸ 图标控制（左键切换，右键菜单）" with title "iPhone 镜像去广告" sound name "Funk"'
	exit 0
fi

[ -x "$PY" ] || { osascript -e 'display notification "缺少 ~/Projects/mcp/.venv，无法启动" with title "iPhone 镜像去广告" sound name "Basso"'; exit 1; }
[ -f "$DIR/src/app.py" ] || { osascript -e 'display notification "找不到 '"$DIR"'/src/app.py" with title "iPhone 镜像去广告" sound name "Basso"'; exit 1; }

cd "$DIR" || exit 1
mkdir -p logs
# SCK 采集器（ScreenCaptureKit 取帧）：不开它就会回退 CG 截图 → 红果不断弹「识图找同款」
if ! pgrep -x sck_grab >/dev/null 2>&1; then
    if [ -x "$DIR/bin/sck_grab" ]; then
        : > logs/sck_grab.log
        nohup "$DIR/bin/sck_grab" >/dev/null 2>&1 &
        sleep 2
    else
        osascript -e 'display notification "缺 bin/sck_grab → 将回退 CG 截图（红果会弹识图浮窗）。编译：swiftc -O -o bin/sck_grab bin/sck_grab.swift" with title "iPhone 镜像去广告"'
    fi
fi
nohup "$PY" src/app.py >> logs/app_start.log 2>&1 &
echo $! > "$PIDFILE"
sleep 3
if running; then
	osascript -e 'display notification "已启动（PID '"$(cat "$PIDFILE")"'）：菜单栏出现 ▶︎ 图标\n左键点 = 开始/暂停，右键点 = 菜单" with title "iPhone 镜像去广告"'
else
	osascript -e 'display notification "启动失败，见 '"$DIR"'/logs/app_start.log" with title "iPhone 镜像去广告" sound name "Basso"'
	rm -f "$PIDFILE"
	exit 1
fi
