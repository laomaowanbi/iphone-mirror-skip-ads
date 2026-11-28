#!/bin/bash
# 把菜单栏监控打包成可双击的 App：~/Applications/iPhone镜像去广告.app
#   用法：bash scripts/make_app.sh
#   版本号取自 src/app.py 里的 VERSION（菜单、tooltip、日志与 App 版本保持一致）
#   签名用 adhoc（-），本机自用不需要开发者证书
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/iPhone镜像去广告.app"
ID="com.nantiange.iphone-mirror-adblock"
VER="$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$DIR/src/app.py" | head -1)"
VER="${VER:-0.0}"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDisplayName</key><string>iPhone镜像去广告</string>
	<key>CFBundleName</key><string>iPhone镜像去广告</string>
	<key>CFBundleExecutable</key><string>launcher</string>
	<key>CFBundleIdentifier</key><string>${ID}</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>${VER}</string>
	<key>CFBundleVersion</key><string>${VER}</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>LSMinimumSystemVersion</key><string>15.0</string>
	<key>LSUIElement</key><true/>
	<key>NSHighResolutionCapable</key><true/>
	<key>NSPrincipalClass</key><string>NSApplication</string>
</dict>
</plist>
PLIST

cp "$DIR/scripts/launcher.sh" "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/MacOS/launcher"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# plist 改过必须重签一次，否则签名与内容不一致
codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "⚠️ codesign 失败（adhoc），本机仍可用"

echo "✅ ${APP}  v${VER} 已生成/更新（双击即可启动，左键点图标 = 开始/暂停，右键 = 菜单）"
