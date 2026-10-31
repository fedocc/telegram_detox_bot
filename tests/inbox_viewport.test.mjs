import assert from 'node:assert/strict';
import test from 'node:test';
import {bindMobileViewport} from '../app/inbox/static/ui.mjs';

function fixture({visual = true, mobile = true} = {}) {
  const listeners = new Map(), frames = new Map(), properties = new Map();
  let nextFrame = 0, writes = 0;
  const target = values => Object.assign({
    addEventListener(name, handler) { listeners.set(this.id + name, handler); },
    removeEventListener(name) { listeners.delete(this.id + name); },
  }, values);
  const query = target({id: 'media:', matches: mobile});
  const viewport = target({id: 'visual:', height: 852, offsetTop: 0, scale: 1});
  const win = target({id: 'window:', innerHeight: 852, visualViewport: visual ? viewport : null,
    matchMedia: () => query,
    requestAnimationFrame(fn) { frames.set(++nextFrame, fn); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); },
  });
  const root = {style: {
    setProperty(name, value) { writes++; properties.set(name, value); },
    removeProperty(name) { properties.delete(name); },
  }};
  const destroy = bindMobileViewport(win, root);
  return {win, viewport, query, properties, frames, listeners, destroy,
    get writes() { return writes; },
    emit: (target, name) => listeners.get(target.id + name)?.(),
    flush() { for (const [id, fn] of frames) { frames.delete(id); fn(); } },
    geometry() { return {top: properties.get('--app-viewport-top'),
      height: properties.get('--app-viewport-height')}; },
  };
}

test('mobile app uses the visible rectangle from the first paint', () => {
  const f = fixture();
  assert.deepEqual(f.geometry(), {top: '0px', height: '852px'});
});

test('keyboard pan and dismissal restore both edges without a leftover bottom gap', () => {
  const f = fixture();
  Object.assign(f.viewport, {height: 480, offsetTop: 88});
  f.emit(f.viewport, 'resize'); f.emit(f.viewport, 'scroll'); f.flush();
  assert.deepEqual(f.geometry(), {top: '88px', height: '480px'});
  Object.assign(f.viewport, {height: 852, offsetTop: 0});
  f.emit(f.viewport, 'resize'); f.flush();
  assert.deepEqual(f.geometry(), {top: '0px', height: '852px'});
});

test('rotation and browser chrome changes do not reuse the old screen height', () => {
  const f = fixture();
  f.viewport.height = 393;
  f.emit(f.win, 'resize'); f.flush();
  assert.equal(f.geometry().height, '393px');
  f.viewport.height = 760;
  f.emit(f.viewport, 'resize'); f.flush();
  assert.equal(f.geometry().height, '760px');
});

test('pinch zoom keeps the original layout instead of reflowing to a zoomed viewport', () => {
  const f = fixture();
  Object.assign(f.viewport, {height: 426, offsetTop: 120, scale: 2});
  f.emit(f.viewport, 'resize'); f.flush();
  assert.deepEqual(f.geometry(), {top: '0px', height: '852px'});
  Object.assign(f.viewport, {height: 852, offsetTop: 0, scale: 1});
  f.emit(f.viewport, 'resize'); f.flush();
  assert.deepEqual(f.geometry(), {top: '0px', height: '852px'});
});

test('desktop resize clears mobile geometry and retains normal desktop layout', () => {
  const f = fixture();
  f.query.matches = false; f.emit(f.query, 'change'); f.flush();
  assert.equal(f.properties.size, 0);
  f.query.matches = true; f.emit(f.query, 'change'); f.flush();
  assert.deepEqual(f.geometry(), {top: '0px', height: '852px'});
});

test('resize bursts coalesce and unchanged geometry does not trigger layout writes', () => {
  const f = fixture(), original = f.writes;
  for (let i = 0; i < 20; i++) f.emit(f.viewport, 'resize');
  assert.equal(f.frames.size, 1); f.flush();
  assert.equal(f.writes, original);
  assert.equal(f.frames.size, 0);
});

test('returning to an installed app refreshes the viewport without requiring a reload', () => {
  const f = fixture();
  f.viewport.height = 810; f.emit(f.win, 'pageshow'); f.flush();
  assert.equal(f.geometry().height, '810px');
});

test('older browsers fall back to window height and invalid transient sizes are ignored', () => {
  const f = fixture({visual: false});
  f.win.innerHeight = 600; f.emit(f.win, 'resize'); f.flush();
  assert.equal(f.geometry().height, '600px');
  f.win.innerHeight = 0; f.emit(f.win, 'resize'); f.flush();
  assert.equal(f.geometry().height, '600px');
});

test('teardown cancels queued layout work and releases listeners', () => {
  const f = fixture();
  f.emit(f.viewport, 'resize'); f.destroy();
  assert.equal(f.frames.size, 0);
  assert.equal(f.listeners.size, 0);
  assert.equal(f.properties.size, 0);
});
