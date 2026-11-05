import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlaybackController} from '../app/inbox/static/playback.mjs';

function fixture() {
  const elements = [], listeners = new Map();
  const root = {
    querySelectorAll: () => elements.filter(m => m.isConnected),
    contains: m => elements.includes(m) && m.isConnected,
    addEventListener: (event, handler, capture) => {
      assert.equal(event, 'play'); assert.equal(capture, true); listeners.set(event, handler);
    },
    removeEventListener: event => listeners.delete(event),
  };
  function media(kind) {
    const element = {kind, paused:true, isConnected:true, pauses:0,
      matches: selector => selector === 'audio, video',
      pause() { this.paused = true; this.pauses++; },
      play() { this.paused = false; listeners.get('play')?.({target:this}); },
      querySelectorAll: () => [], contains: other => other === element,
    };
    elements.push(element); return element;
  }
  return {media, root, listeners, controller:createPlaybackController(root)};
}

for (const from of ['voice', 'audio', 'video', 'video_note']) {
  for (const to of ['voice', 'audio', 'video', 'video_note']) {
    test(`${from} -> ${to} pauses the previous item`, () => {
      const f = fixture(), a = f.media(from), b = f.media(to);
      a.play(); assert.equal(a.paused, false);
      b.play(); assert.equal(a.paused, true); assert.equal(b.paused, false);
    });
  }
}

test('no autoplay; a queued play event from a paused item cannot steal playback', () => {
  const f = fixture(), a = f.media('voice'), b = f.media('video');
  assert.ok(a.paused && b.paused);
  a.play(); b.play(); f.listeners.get('play')({target:a});
  assert.ok(a.paused); assert.equal(b.paused, false);
});

test('conversation clearing and bubble replacement pause media before removal', () => {
  const f = fixture(), a = f.media('video_note'); a.play();
  const bubble = {querySelectorAll: () => [a], contains: m => m === a};
  f.controller.pauseWithin(bubble); assert.ok(a.paused);
  a.play(); f.controller.pauseWithin(f.root); assert.ok(a.paused);
});

test('detached playback owner is paused even though no longer in the document', () => {
  const f = fixture(), a = f.media('voice'), b = f.media('audio');
  a.play(); a.isConnected = false; b.play();
  assert.ok(a.paused); assert.equal(b.paused, false);
  b.isConnected = false; f.controller.stopAll(); assert.ok(b.paused);
});

test('a removed element with a pending play event cannot start playback', () => {
  const f = fixture(), a = f.media('video'); a.isConnected = false; a.play();
  assert.ok(a.paused);
});

test('dispose pauses playback and removes the global listener', () => {
  const f = fixture(), a = f.media('audio'); a.play(); f.controller.dispose();
  assert.ok(a.paused); assert.equal(f.listeners.size, 0);
});

const {playbackFailureMessage} = await import('../app/inbox/static/playback.mjs');

test('an expired chat is reported as expired access rather than an unsupported codec', async () => {
  const result = await playbackFailureMessage({src:'/media', isConnected:true},
    {name:'NotSupportedError'}, async () => ({ok:false,status:404,
      json:async () => ({error:'Разговор закрыт или время активности истекло.'})}));
  assert.equal(result,'Разговор закрыт или время активности истекло.');
});

test('an upstream failure is reported as a server failure', async () => {
  const result = await playbackFailureMessage({src:'/media', isConnected:true},
    {name:'NotSupportedError'}, async () => ({ok:false,status:503,json:async () => ({})}));
  assert.match(result,/с сервера/);
  assert.doesNotMatch(result,/браузере/);
});

test('a pause or removed player does not become a false media error or make a request', async () => {
  const noRequest = async () => { throw new Error('unexpected request'); };
  assert.equal(await playbackFailureMessage({isConnected:true},{name:'AbortError'},noRequest),null);
  assert.equal(await playbackFailureMessage({isConnected:false},{name:'NotSupportedError'},noRequest),null);
});

test('media error probe stays bounded and releases its response', async () => {
  let cancelled = false;
  const result = await playbackFailureMessage({src:'/media', isConnected:true},
    {name:'NotSupportedError'}, async (url, options) => {
      assert.equal(url,'/media'); assert.equal(options.headers.Range,'bytes=0-0');
      assert.equal(options.cache,'no-store');
      return {ok:true,body:{cancel:async () => {cancelled=true;}}};
    });
  assert.ok(cancelled); assert.match(result,/Попробуйте ещё раз/);
});
