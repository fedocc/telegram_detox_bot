'use strict';

const CACHE = 'telegram-detox-shell-v9';
const SHELL = new Set([
  '/',
  '/manifest.webmanifest',
  '/static/style.css?v=9',
  '/static/app.js?v=9',
  '/static/ui.mjs?v=9',
  '/static/playback.mjs?v=9',
  '/static/notifications.mjs?v=9',
  '/static/push.mjs?v=9',
  '/static/app-icon-180.png',
  '/static/app-icon-512.png',
]);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([...SHELL])));
  self.skipWaiting();
});

const validConversation = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);

self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data?.json() || {}; } catch (_) {}
  const conversation = validConversation(payload.conversation_id) ? payload.conversation_id : null;
  const tag = conversation ? `conversation:${conversation}` : 'telegram-detox:attention';
  const url = conversation ? `/?conversation=${conversation}` : '/';
  const title = typeof payload.title === 'string' ? payload.title.slice(0, 100) : 'Telegram Detox';
  const subtitle = typeof payload.subtitle === 'string' ? payload.subtitle.slice(0, 100) : 'Новое сообщение';
  const body = typeof payload.body === 'string' ? payload.body.slice(0, 200) : subtitle;
  const badge = Number.isSafeInteger(payload.badge) && payload.badge > 0 ? payload.badge : 0;
  event.waitUntil(Promise.all([
    self.registration.showNotification(title, {
      body: `${subtitle}${body ? `\n${body}` : ''}`,
      tag, renotify: true, icon: '/static/app-icon-180.png',
      data: {url},
    }),
    typeof self.navigator?.setAppBadge === 'function'
      ? self.navigator.setAppBadge(badge).catch(() => {}) : Promise.resolve(),
  ]));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const raw = event.notification.data?.url;
  const match = typeof raw === 'string' ? raw.match(/^\/\?conversation=([a-f0-9]{32})$/) : null;
  const url = match ? `/?conversation=${match[1]}` : '/';
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
    if (clients.length) {
      const client = clients[0];
      if ('navigate' in client) await client.navigate(url);
      return client.focus();
    }
    return self.clients.openWindow(url);
  }));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE).map(key => caches.delete(key)),
  )));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  // API data, Telegram messages/media and search responses are always network-only.
  const shellKey = `${url.pathname}${url.search}`;
  if (url.pathname.startsWith('/api/')
      || !(SHELL.has(shellKey) || SHELL.has(url.pathname))) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(request, copy)));
    }
    return response;
  }).catch(() => caches.match(request)));
});
