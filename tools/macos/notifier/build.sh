#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$source_dir/../../.." && pwd)"
name="Telegram Inbox Notifier"
flags=(-D NOTIFIER_PRODUCTION)
case "${1:-}" in
  --qa) name="Telegram Inbox Notifier QA"; flags=(-D NOTIFIER_QA) ;;
  "") ;;
  *) exit 2 ;;
esac
bundle="$repo/build/$name.app"
icon_source="$repo/app/inbox/static/app-icon-512.png"
iconset="$(mktemp -d)/DetoxIcon.iconset"
trap 'rm -rf "$(dirname "$iconset")"' EXIT
rm -rf "$bundle"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources" "$iconset"
for spec in "16:icon_16x16.png" "32:icon_16x16@2x.png" \
  "32:icon_32x32.png" "64:icon_32x32@2x.png" \
  "128:icon_128x128.png" "256:icon_128x128@2x.png" \
  "256:icon_256x256.png" "512:icon_256x256@2x.png" \
  "512:icon_512x512.png" "1024:icon_512x512@2x.png"; do
  size="${spec%%:*}"
  output="${spec#*:}"
  /usr/bin/sips -z "$size" "$size" "$icon_source" --out "$iconset/$output" >/dev/null
done
/usr/bin/iconutil -c icns "$iconset" -o "$bundle/Contents/Resources/DetoxIcon.icns"
xcrun swiftc "${flags[@]}" -swift-version 5 -O -framework AppKit -framework UserNotifications -framework Network \
  "$source_dir/Core.swift" "$source_dir/main.swift" -o "$bundle/Contents/MacOS/TelegramInboxNotifier"
cp "$source_dir/Info.plist" "$bundle/Contents/Info.plist"
if [ "${1:-}" = --qa ]; then
  /usr/bin/plutil -replace CFBundleIdentifier -string com.fedocc.telegram-inbox-notifier.qa "$bundle/Contents/Info.plist"
  /usr/bin/plutil -replace CFBundleName -string "$name" "$bundle/Contents/Info.plist"
  /usr/bin/plutil -replace CFBundleDisplayName -string "$name" "$bundle/Contents/Info.plist"
fi
/usr/bin/codesign --force --sign - "$bundle"
printf '%s\n' "$bundle"
