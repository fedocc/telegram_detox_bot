import assert from 'node:assert/strict';
import test from 'node:test';
import {createMessageViewport} from '../app/inbox/static/ui.mjs';

function viewportFixture() {
  const handlers = new Map(), frames = [], targets = new Set();
  let position = 600, writes = 0, resize;
  const pane = {
    scrollHeight: 1000, clientHeight: 400, children: [{id: 'message'}],
    get scrollTop() { return position; },
    set scrollTop(value) {
      writes++; position = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
    },
    addEventListener(name, fn) { handlers.set(name, fn); },
    removeEventListener(name) { handlers.delete(name); },
  };
  const viewport = createMessageViewport(pane, {
    scheduleFrame: fn => frames.push(fn),
    observeResize: fn => {
      resize = fn;
      return {observe: x => targets.add(x), unobserve: x => targets.delete(x),
        disconnect: () => targets.clear()};
    },
  });
  viewport.watch();
  return {pane, viewport, frames, targets, handlers, resize: () => resize(),
    emit: (name, event = {}) => handlers.get(name)?.(event),
    flush: () => { while (frames.length) frames.shift()(); },
    get writes() { return writes; }};
}

test('late media still follows the bottom after the opening frames have finished', () => {
  const f = viewportFixture();
  f.viewport.follow(); f.flush();
  f.pane.scrollHeight += 693; f.resize();
  assert.equal(f.pane.scrollTop, 1293);
  assert.equal(f.pane.scrollHeight - f.pane.scrollTop - f.pane.clientHeight, 0);
});

test('scrolling up cancels pending opening frames and late-media following', () => {
  const f = viewportFixture();
  f.viewport.follow();
  f.emit('wheel', {deltaY: -30});
  f.pane.scrollTop = 570; f.emit('scroll');
  f.pane.scrollHeight += 200; f.resize(); f.flush();
  assert.equal(f.viewport.following, false);
  assert.equal(f.pane.scrollTop, 570);
});

test('reading even one line above the bottom stays put until the reader returns', () => {
  const f = viewportFixture();
  f.viewport.follow(); f.flush();
  f.pane.scrollTop = 580; f.emit('scroll');
  f.pane.scrollHeight += 100; f.resize();
  assert.equal(f.pane.scrollTop, 580);
  f.pane.scrollTop = 700; f.emit('scroll');
  f.pane.scrollHeight += 50; f.resize();
  assert.equal(f.pane.scrollTop, 750);
});

test('a previous chat cannot scroll a newly selected chat in its old animation frame', () => {
  const f = viewportFixture();
  f.viewport.follow();
  const oldFrame = f.frames.shift();
  f.viewport.reset(); f.viewport.watch(); f.viewport.follow();
  f.pane.scrollTop = 123;
  oldFrame();
  assert.equal(f.pane.scrollTop, 123);
  f.flush();
  assert.equal(f.pane.scrollTop, 600);
});

test('an explicit jump to a quoted message is not overridden by opening or media', () => {
  const f = viewportFixture();
  f.viewport.follow(); f.viewport.release();
  f.pane.scrollTop = 150;
  f.pane.scrollHeight += 100; f.resize(); f.flush();
  assert.equal(f.pane.scrollTop, 150);
});

test('touch and keyboard navigation release the bottom before a pending layout change', () => {
  for (const gesture of ['touch', 'keyboard']) {
    const f = viewportFixture(); f.viewport.follow();
    if (gesture === 'touch') {
      f.emit('touchstart', {touches: [{clientY: 100}]});
      f.emit('touchmove', {touches: [{clientY: 150}]});
    } else f.emit('keydown', {key: 'PageUp'});
    f.pane.scrollTop = 400; f.pane.scrollHeight += 200; f.resize(); f.flush();
    assert.equal(f.pane.scrollTop, 400);
  }
});

test('composer and viewport resizing preserve the latest message without an observer loop', () => {
  const f = viewportFixture(); f.viewport.follow(); f.flush();
  f.pane.clientHeight = 300; f.resize();
  assert.equal(f.pane.scrollTop, 700);
  const before = f.writes;
  for (let i = 0; i < 10; i++) f.resize();
  assert.equal(f.writes, before);
});

test('replaced message nodes are unobserved and teardown releases all listeners', () => {
  const f = viewportFixture(), previous = f.pane.children[0], next = {id: 'replacement'};
  f.pane.children = [next]; f.viewport.watch();
  assert.equal(f.targets.has(previous), false);
  assert.deepEqual([...f.targets], [f.pane, next]);
  f.viewport.destroy();
  assert.equal(f.targets.size, 0);
  assert.equal(f.handlers.size, 0);
});
