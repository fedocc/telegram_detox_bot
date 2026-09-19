#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode


def replace_line(text: str, name: str, value: str) -> str:
    lines = text.splitlines()
    replacement = f"{name}={value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    env_path = root / ".env"
    if not env_path.is_file() or env_path.is_symlink():
        raise SystemExit("Refusing: expected a regular existing .env file.")
    existing = env_path.read_text(encoding="utf-8")
    if any(line.startswith("WEB_PUSH_VAPID_PRIVATE_KEY=") and line.split("=", 1)[1].strip()
           for line in existing.splitlines()):
        print("Web Push VAPID key already configured; no change made.")
        return
    vapid = Vapid()
    vapid.generate_keys()
    private_number = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    private_key = b64urlencode(private_number)
    public_key = b64urlencode(vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    ))
    updated = replace_line(existing, "WEB_PUSH_VAPID_PRIVATE_KEY", private_key)
    temporary = env_path.with_name(".env.web-push.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, env_path)
        env_path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Web Push configured. Public VAPID key: {public_key}")


if __name__ == "__main__":
    main()
