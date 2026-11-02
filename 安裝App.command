#!/bin/zsh
# 安裝「語音輸入」（只要做一次）：下載需要的套件，產生「語音輸入.app」。之後雙擊 App 就會在背景執行。
# 這個分享版不用裝 Xcode 開發工具：蘋果辨識小幫手（app/apple-speech）和 App 啟動程式（app/voice-type-launcher）
# 已經先編譯好了，原始碼就在旁邊（apple_speech.swift、launcher.c）。
# 注意：重新安裝之後，macOS 會把 App 當成新的，權限要重新打開一次。
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP="語音輸入.app"

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "❌ 這台 Mac 不是 Apple 晶片（M1 以後），語音輸入不能用。"
  exit 1
fi
if (( $(sw_vers -productVersion | cut -d. -f1) < 26 )); then
  echo "❌ 需要 macOS 26 以上，這台是 $(sw_vers -productVersion)。請先到「系統設定 → 一般 → 軟體更新」更新。"
  exit 1
fi

# 從網路下載的檔案會被 macOS 加上「隔離」標記，雙擊 App 時會被擋下來；這些是你自己要安裝的檔案，先把標記拿掉
xattr -dr com.apple.quarantine . 2>/dev/null || true

if ! command -v uv >/dev/null; then
  echo "下載 uv（免費的 Python 套件管理工具）…"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

# 沒裝開發工具的 Mac，/usr/bin 裡的 python3、install_name_tool 等只是替身，一執行就會跳出「要安裝開發工具嗎」的視窗。
# 所以只用 uv 自己下載的 Python，執行 uv 時也不給它 /usr/bin，它找不到這些工具就會略過（不影響使用）
UV="$(command -v uv)"
echo "下載 Python…"
# 略過時 uv 會印一段「請安裝開發工具」的英文警告，不用理它，所以只有真的失敗才顯示
if ! output="$(PATH="/bin:/usr/sbin:/sbin" "$UV" python install 3.12 2>&1)"; then
  echo "$output"
  echo "❌ 下載 Python 失敗，請確認網路，再執行一次。"
  exit 1
fi

echo "下載需要的套件（約 1 分鐘，請稍等）…"
PATH="/bin:/usr/sbin:/sbin" "$UV" sync --python-preference only-managed

echo "產生 $APP …"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp app/voice-type-launcher "$APP/Contents/MacOS/voice-type"
cp app/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.voice-type</string>
  <key>CFBundleName</key><string>語音輸入</string>
  <key>CFBundleDisplayName</key><string>語音輸入</string>
  <key>CFBundleExecutable</key><string>voice-type</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>語音輸入需要使用麥克風，把你說的話轉成文字（全部在這台電腦上處理，不會上傳）。</string>
</dict>
</plist>
PLIST
codesign --force --sign - "$APP"
touch "$APP"
echo ""
echo "✅ 完成！雙擊資料夾裡的「$APP」就可以啟動。"
echo "   （$APP 要留在這個資料夾裡，想放 Dock 的話把它拖到 Dock 就好）"
