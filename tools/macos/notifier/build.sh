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
mkdir -p "$bundle/Contents/MacOS"
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
