// One playback owner across voice, ordinary audio, video and video notes.
// Capture is required: the HTML media "play" event does not bubble.
export function createPlaybackController(root) {
  let active = null;
  const isMedia = element => element?.matches?.('audio, video');

  function pauseWithin(container) {
    if (isMedia(container)) container.pause();
    for (const media of container.querySelectorAll('audio, video')) media.pause();
    if (active && (!active.isConnected || container.contains(active))) {
      active.pause();
      active = null;
    }
  }

  function onPlay(event) {
    const next = event.target;
    // Ignore queued play events from an element already paused or removed.
    if (!isMedia(next) || next.paused) return;
    if (!next.isConnected) { next.pause(); return; }
    if (active && active !== next) active.pause();
    for (const media of root.querySelectorAll('audio, video')) {
      if (media !== next && !media.paused) media.pause();
    }
    active = next;
  }

  root.addEventListener('play', onPlay, true);
  return {
    pauseWithin,
    stopAll() { pauseWithin(root); },
    dispose() {
      pauseWithin(root);
      root.removeEventListener('play', onPlay, true);
    },
  };
}

// Native players also report HTTP failures as unsupported media. Ask the same
// protected URL for a tiny range before deciding what to tell the user.
export async function playbackFailureMessage(media, error, fetcher = fetch) {
  if (error?.name === 'AbortError' || media.isConnected === false) return null;
  if (error?.name === 'NotAllowedError') return 'Нажмите воспроизведение ещё раз, чтобы включить звук.';
  try {
    const response = await fetcher(media.currentSrc || media.src, {
      cache: 'no-store', headers: {Range: 'bytes=0-0'},
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if ([403, 404, 410, 413, 429].includes(response.status) && body.error) return body.error;
      return 'Не удалось загрузить вложение с сервера. Попробуйте ещё раз.';
    }
    // Do not retain or download the rest of a server response to this probe.
    await response.body?.cancel();
  } catch (_) {
    return 'Нет соединения с сервисом. Проверьте сеть и попробуйте ещё раз.';
  }
  if (media.error?.code === 3) return 'Не удалось декодировать файл. Его можно скачать по ссылке ниже.';
  return 'Не удалось воспроизвести файл. Попробуйте ещё раз или скачайте его.';
}
