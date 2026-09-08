#!/bin/bash
set -euo pipefail
umask 077
source_dir="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$source_dir/../../.." && pwd)"
"$source_dir/build.sh"
root="$HOME/Library/Application Support/TelegramMentionInbox"
label=com.fedocc.telegram-inbox-notifier
plist="$HOME/Library/LaunchAgents/$label.plist"
bundle="$root/Telegram Inbox Notifier.app"
mkdir -p "$root" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$root"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
/usr/bin/ditto "$repo/build/Telegram Inbox Notifier.app" "$bundle"
/usr/bin/plutil -create xml1 "$plist"
/usr/bin/plutil -insert Label -string "$label" "$plist"
/usr/bin/plutil -insert ProgramArguments -xml '<array/>' "$plist"
/usr/bin/plutil -insert ProgramArguments.0 -string "$bundle/Contents/MacOS/TelegramInboxNotifier" "$plist"
/usr/bin/plutil -insert RunAtLoad -bool YES "$plist"
/usr/bin/plutil -insert KeepAlive -bool YES "$plist"
/usr/bin/plutil -insert ThrottleInterval -integer 15 "$plist"
/usr/bin/plutil -insert LimitLoadToSessionType -string Aqua "$plist"
/usr/bin/plutil -insert StandardOutPath -string "$HOME/Library/Logs/telegram-inbox-notifier.log" "$plist"
/usr/bin/plutil -insert StandardErrorPath -string "$HOME/Library/Logs/telegram-inbox-notifier-error.log" "$plist"
chmod 600 "$plist"
touch "$HOME/Library/Logs/telegram-inbox-notifier.log" "$HOME/Library/Logs/telegram-inbox-notifier-error.log"
chmod 600 "$HOME/Library/Logs/telegram-inbox-notifier.log" "$HOME/Library/Logs/telegram-inbox-notifier-error.log"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$bundle"
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl kickstart "gui/$(id -u)/$label"
printf 'Installed Telegram Inbox Notifier. Allow notifications in the macOS prompt.\n'
