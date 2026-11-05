// Private photos live only in this page's memory; polling reuses pending downloads.
export function createAvatars(document) {
  const cache = new Map(), targets = new WeakMap(), waiting = [];
  let running = 0;
  const pump = () => {
    while (running < 3 && waiting.length) {
      running++;
      waiting.shift()().finally(() => { running--; pump(); });
    }
  };
  const load = url => {
    const existing = cache.get(url);
    if (existing && existing.expires > Date.now()) {
      cache.delete(url); cache.set(url, existing);
      return existing.promise;
    }
    if (existing?.image) URL.revokeObjectURL(existing.image);
    const entry = {expires: Infinity, image: null, promise: null};
    entry.promise = new Promise(resolve => {
      waiting.push(async () => {
        if (cache.get(url) !== entry) { resolve(null); return; }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 30000);
        try {
          const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
          if (response.status === 200 && response.headers.get('Content-Type')?.startsWith('image/jpeg')) {
            entry.image = URL.createObjectURL(await response.blob());
          }
        } catch (_) { /* Initials remain visible when photos are hidden or unavailable. */ }
        finally {
          clearTimeout(timer);
          entry.expires = Date.now() + (entry.image ? 300000 : 60000);
          if (cache.get(url) !== entry && entry.image) {
            URL.revokeObjectURL(entry.image); entry.image = null;
          }
          resolve(entry.image);
        }
      });
    });
    cache.set(url, entry);
    while (cache.size > 200) {
      const key = cache.keys().next().value, old = cache.get(key);
      if (old.image) URL.revokeObjectURL(old.image);
      cache.delete(key);
    }
    pump();
    return entry.promise;
  };
  const show = async element => {
    const target = targets.get(element);
    if (!target || !element.isConnected) return;
    const image = await load(target.url);
    if (!element.isConnected || targets.get(element) !== target) return;
    const previous = element.querySelector('img');
    if (!image) { previous?.remove(); return; }
    if (previous?.getAttribute('src') === image) return;
    const img = document.createElement('img');
    img.alt = ''; img.src = image; img.decoding = 'async'; img.draggable = false;
    img.referrerPolicy = 'no-referrer';
    try { if (img.decode) await img.decode(); } catch (_) { return; }
    if (!element.isConnected || targets.get(element) !== target) return;
    img.onerror = () => img.remove();
    if (previous?.parentNode === element) previous.replaceWith(img);
    else element.append(img);
  };
  const observer = typeof IntersectionObserver !== 'undefined' ? new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target); show(entry.target);
    }
  }, {rootMargin: '100px'}) : null;
  return {
    set(element, title, url, eager = false) {
      const previous = targets.get(element), cached = cache.get(url);
      if (previous?.url === url && previous.title === title && cached?.expires > Date.now()) return;
      observer?.unobserve(element);
      const samePhoto = previous?.url === url;
      if (!samePhoto || previous.title !== title) {
        const picture = samePhoto ? element.querySelector('img') : null;
        element.textContent = String(title || '').trim().split(/\s+/).slice(0, 2)
          .map(value => value[0] || '').join('').toUpperCase();
        if (picture) element.append(picture);
      }
      if (!url || !/^\/api\//.test(url)) { targets.delete(element); return; }
      targets.set(element, {url, title});
      if (eager || !observer) show(element); else observer.observe(element);
    },
    forgetWithin(element) {
      for (const avatar of [element, ...element.querySelectorAll('.avatar')]) {
        observer?.unobserve(avatar); targets.delete(avatar);
      }
    },
  };
}
