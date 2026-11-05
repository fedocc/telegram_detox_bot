import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../app/inbox/static/sw.js', import.meta.url), 'utf8');
function worker() {
  const events = new Map(), shown = [], navigated = [], badges = [];
  const self = {
    addEventListener: (name, handler) => events.set(name, handler),
    navigator: {setAppBadge: async value => badges.push(value), clearAppBadge: async () => {}},
    registration: {showNotification: async (title, options) => shown.push({title, options})},
    clients: {matchAll: async () => [], openWindow: async url => navigated.push(url)},
  };
  vm.runInNewContext(source, {self, Set, URL, Promise});
  return {shown, navigated, badges,
    async push(payload) {
      let done;
      events.get('push')({data: {json: () => payload}, waitUntil: p => {done = p;}});
      await done;
    },
    async click(url) {
      let done;
      events.get('notificationclick')({notification: {data: {url}, close() {}},
        waitUntil: p => {done = p;}});
      await done;
    },
  };
}

test('digest push stays short, uses a stable tag and opens only the digest route', async () => {
  const w = worker();
  await w.push({kind:'digest', title:'Сводка готова', body:'x'.repeat(500),
    url:'https://untrusted.test', badge:3});
  assert.equal(w.shown[0].options.body.length, 90);
  assert.equal(w.shown[0].options.tag, 'telegram-detox:digest');
  assert.equal(w.shown[0].options.data.url, '/?view=digests');
  assert.deepEqual(w.badges, [3]);
  await w.click(w.shown[0].options.data.url);
  assert.deepEqual(w.navigated, ['/?view=digests']);
});

test('normal chat notifications keep their own grouping and deep link', async () => {
  const w = worker(), conversation = 'a'.repeat(32);
  await w.push({conversation_id:conversation, body:'Голосовое сообщение'});
  assert.equal(w.shown[0].options.tag, `conversation:${conversation}`);
  await w.click(w.shown[0].options.data.url);
  assert.deepEqual(w.navigated, [`/?conversation=${conversation}`]);
});

test('digest click rejects external URLs and extra untrusted parameters', async () => {
  const w = worker();
  for (const url of ['https://untrusted.test/?view=digests', '/?view=digests&next=evil',
    '/?conversation=bad']) await w.click(url);
  assert.deepEqual(w.navigated, ['/', '/', '/']);
});
