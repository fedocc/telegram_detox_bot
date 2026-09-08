#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")" && pwd)"
output="$(mktemp -d)"
trap 'rm -rf "$output"' EXIT
xcrun swiftc -swift-version 5 "$source_dir/Core.swift" "$source_dir/CoreTests.swift" -o "$output/core-tests"
"$output/core-tests"
