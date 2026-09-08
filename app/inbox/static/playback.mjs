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
