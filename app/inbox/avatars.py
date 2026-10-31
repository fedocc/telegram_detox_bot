"""Bounded private cache of the current profile photo, never photo history."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import time
import warnings
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.inbox.errors import InboxError

MAX_AVATAR_DOWNLOAD = 2 * 1024 * 1024
MAX_AVATAR_CACHE = 32 * 1024 * 1024
AVATAR_TTL = 300


class BoundedPhoto(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_AVATAR_DOWNLOAD:
            raise InboxError("Фотография слишком большая.", 413)
        return super().write(data)


def avatar_jpeg(raw):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 4096 * 4096:
                    raise ValueError("Image dimensions exceed avatar limit")
                image = ImageOps.exif_transpose(source)
                image.thumbnail((512, 512))
                output = io.BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=85, optimize=True)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise InboxError("Фотография недоступна.", 404) from None


class AvatarCache:
    def __init__(self, client, directory, clock=time.time):
        self.client, self.directory, self.clock = client, Path(directory), clock
        if self.directory.is_symlink():
            raise ValueError("Avatar directory must not be a symlink")
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self.entries = OrderedDict()
        self.locks = [asyncio.Lock() for _ in range(32)]
        self.slots = asyncio.Semaphore(3)
        for path in self.directory.glob("*.part"):
            path.unlink(missing_ok=True)
        self.prune()

    def prune(self):
        files = sorted(self.directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and (len(files) > 256 or total > MAX_AVATAR_CACHE):
            path = files.pop(0)
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
        while len(self.entries) > 256:
            key, _ = self.entries.popitem(last=False)
            (self.directory / f"{key}.jpg").unlink(missing_ok=True)

    async def get(self, identity, get_entity):
        key = hashlib.sha256(str(identity).encode()).hexdigest()[:32]
        path = self.directory / f"{key}.jpg"
        async with self.locks[int(key[:2], 16) % len(self.locks)]:
            cached = self.entries.get(key)
            if path.is_symlink():
                raise InboxError("Фотография недоступна.", 404)
            if cached and cached[0] > self.clock():
                self.entries.move_to_end(key)
                if cached[1] is None:
                    return None
                if path.exists():
                    return path.read_bytes()
            async with self.slots:
                entity = await get_entity()
                photo = getattr(entity, "photo", None)
                version = getattr(photo, "photo_id", None) or getattr(photo, "id", None)
                if not version:
                    path.unlink(missing_ok=True)
                    self.entries[key] = (self.clock() + 60, None)
                    self.prune()
                    return None
                if cached and cached[1] == version and path.exists():
                    self.entries[key] = (self.clock() + AVATAR_TTL, version)
                    self.entries.move_to_end(key)
                    return path.read_bytes()
                buffer = BoundedPhoto()
                async with asyncio.timeout(30):
                    await self.client.download_profile_photo(entity, file=buffer, download_big=True)
                if not buffer.getvalue():
                    path.unlink(missing_ok=True)
                    self.entries[key] = (self.clock() + 60, None)
                    self.prune()
                    return None
                content = await asyncio.to_thread(avatar_jpeg, buffer.getvalue())
                descriptor, temporary = tempfile.mkstemp(suffix=".part", dir=self.directory)
                temporary = Path(temporary)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(content)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.entries[key] = (self.clock() + AVATAR_TTL, version)
                self.entries.move_to_end(key)
                self.prune()
                return content
