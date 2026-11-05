import assert from 'node:assert/strict';
import test from 'node:test';
import {createAvatars} from '../app/inbox/static/avatars.mjs';

const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  let decode = async () => {}, requests = 0;
  const originalFetch = globalThis.fetch, originalNow = Date.now;
  let now = 0;
  Date.now = () => now;
  globalThis.fetch = async () => {
    requests++;
    return {status: 200, headers: {get: () => 'image/jpeg'},
      blob: async () => new Blob(['synthetic photo'])};
  };
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.isConnected = true; }
    set textContent(value) { this.text = value; this.children.forEach(c => { c.parentNode = null; }); this.children = []; }
    get textContent() { return this.text || ''; }
    append(child) { child.remove(); child.parentNode = this; this.children.push(child); }
    remove() {
      if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
      this.parentNode = null;
    }
    replaceWith(child) {
      const parent = this.parentNode, index = parent.children.indexOf(this);
      child.remove(); parent.children[index] = child; child.parentNode = parent; this.parentNode = null;
    }
    querySelector(tag) { return this.children.find(child => child.tag === tag) || null; }
    querySelectorAll() { return []; }
    getAttribute(name) { return this[name] || null; }
    decode() { return decode(); }
  }
  const document = {createElement: tag => new Element(tag)};
  return {avatars: createAvatars(document), element: new Element('span'),
    requests: () => requests, advance: () => { now += 300001; },
    decoding: value => { decode = value; },
    restore() { globalThis.fetch = originalFetch; Date.now = originalNow; }};
}

test('repeated avatar updates retain the same decoded image without another request', async () => {
  const f = fixture();
  try {
    f.avatars.set(f.element, 'Контакт', '/api/conversations/test/photo', true);
    await tick();
    const photo = f.element.querySelector('img');
    assert.ok(photo);
    for (let i = 0; i < 5; i++) f.avatars.set(f.element, 'Контакт', '/api/conversations/test/photo', true);
    await tick();
    assert.equal(f.element.querySelector('img'), photo);
    assert.equal(f.requests(), 1);
  } finally { f.restore(); }
});

test('cache refresh keeps the current avatar until its replacement is decoded', async () => {
  const f = fixture();
  try {
    f.avatars.set(f.element, 'Контакт', '/api/conversations/test/photo', true);
    await tick();
    const previous = f.element.querySelector('img');
    let ready;
    f.decoding(() => new Promise(resolve => { ready = resolve; }));
    f.advance();
    f.avatars.set(f.element, 'Контакт', '/api/conversations/test/photo', true);
    await tick();
    assert.equal(f.element.querySelector('img'), previous);
    ready(); await tick();
    assert.notEqual(f.element.querySelector('img'), previous);
    assert.equal(f.element.children.length, 1);
    assert.equal(f.requests(), 2);
  } finally { f.restore(); }
});

test('a removed avatar cannot receive an image after asynchronous decoding', async () => {
  const f = fixture();
  try {
    let ready;
    f.decoding(() => new Promise(resolve => { ready = resolve; }));
    f.avatars.set(f.element, 'Контакт', '/api/conversations/test/photo', true);
    await tick();
    f.avatars.forgetWithin(f.element);
    ready(); await tick();
    assert.equal(f.element.querySelector('img'), null);
  } finally { f.restore(); }
});
