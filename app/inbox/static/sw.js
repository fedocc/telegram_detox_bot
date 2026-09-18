'use strict';

const CACHE = 'telegram-detox-shell-v2';
const SHELL = new Set([
  '/',
  '/manifest.webmanifest',
  '/static/style.css',
  '/static/app.js',
  '/static/ui.mjs',
  '/static/playback.mjs',
  '/static/notifications.mjs',
  '/static/app-icon-180.png',
  '/static/app-icon-512.png',
]);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([...SHELL])));
  self.skipWaiting();
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
  if (url.pathname.startsWith('/api/') || !SHELL.has(url.pathname)) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(request, copy)));
    }
    return response;
  }).catch(() => caches.match(request)));
});
