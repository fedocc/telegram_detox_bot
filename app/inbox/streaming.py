"""Range-aware media delivery without waiting for a complete Telegram download."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from telethon.errors import FloodWaitError

from app.inbox.errors import InboxError


async def stream_media(request, service, scope, key, message_id):
    from app.inbox.service import INLINE_MEDIA_TYPES, MAX_MEDIA

    if scope == "digest":
        target, message = await service.digest_message(key, message_id)
    else:
        _, message = await service.interaction_message(scope, key, message_id)
    file = getattr(message, "file", None)
    size = getattr(file, "size", 0) or 0
    if not size:
        raise InboxError("Вложение недоступно.", 404)
    if size > MAX_MEDIA:
        raise InboxError("Вложение превышает лимит 64 МБ.", 413)
    try:
        span = request.http_range
        raw_range = request.headers.get("Range", "")
        if raw_range.startswith("bytes=-") and int(raw_range[7:]) == 0:
            raise ValueError
        start, stop, _ = span.indices(size)
        if start >= size or stop <= start:
            raise ValueError
    except ValueError:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    mime = file.mime_type or "application/octet-stream"
    inline = mime in INLINE_MEDIA_TYPES
    name = quote(Path(file.name or "attachment").name[:180], safe="")
    headers = {
        "Accept-Ranges": "bytes", "Content-Length": str(stop - start),
        "Content-Type": mime if inline else "application/octet-stream",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{name}",
    }
    partial = "Range" in request.headers
    if partial:
        headers["Content-Range"] = f"bytes {start}-{stop - 1}/{size}"
    response = web.StreamResponse(status=206 if partial else 200, headers=headers)
    if request.method == "HEAD":
        await response.prepare(request)
        return response
    # Bound concurrent transfers, but a video never holds the photo/cache lock.
    def revalidate():
        if scope != "digest":
            service.interaction_target(scope, key)
        else:
            source = service.store.library_source(key, enabled_only=False)
            if (str(target.peer_id) in service.store.ignored()
                    or (source is not None and not source.library_enabled)):
                raise InboxError("Источник отключён.", 404)

    async with service.stream_slots:
        revalidate()
        offset = start - start % 4096
        skip, remaining = start - offset, stop - start
        stream = service.client.iter_download(
            message.media, offset=offset,
            request_size=min(512 * 1024, max(4096, ((stop - offset + 4095) // 4096) * 4096)),
            file_size=size,
        )
        try:
            async with asyncio.timeout(300):
                async for chunk in stream:
                    revalidate()
                    chunk = bytes(chunk)
                    if skip:
                        skipped = min(skip, len(chunk))
                        chunk, skip = chunk[skipped:], skip - skipped
                    chunk = chunk[:remaining]
                    if not chunk:
                        continue
                    if not response.prepared:
                        await response.prepare(request)
                    await response.write(chunk)
                    remaining -= len(chunk)
                    if remaining == 0:
                        break
                if remaining:
                    raise OSError("Incomplete media")
            await response.write_eof()
        except (OSError, TimeoutError, InboxError, FloodWaitError) as exc:
            if response.prepared:
                response.force_close()
                if request.transport:
                    request.transport.close()
                return response
            if isinstance(exc, FloodWaitError):
                raise service._flood_wait_error(exc) from None
            raise
        finally:
            await stream.close()
    return response
