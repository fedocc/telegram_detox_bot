import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import {bindReplyNavigation, scopePath} from '../app/inbox/static/ui.mjs';

function quoteElement() {
  return {attributes: {}, setAttribute(name, value) { this.attributes[name] = value; }};
}

test('reply quotes open the original by click, Enter and Space', () => {
  const quote = quoteElement(), opened = [];
  bindReplyNavigation(quote, 7, id => opened.push(id));
  assert.equal(quote.attributes.role, 'button');
  assert.equal(quote.attributes['aria-label'], 'Открыть исходное сообщение');
  assert.equal(quote.tabIndex, 0);
  quote.onclick({target: quote});
  let prevented = 0;
  for (const key of ['Enter', ' ', 'Escape']) {
    quote.onkeydown({target: quote, key, preventDefault() { prevented += 1; }});
  }
  assert.deepEqual(opened, [7, 7, 7]);
  assert.equal(prevented, 2);
});

test('links inside a reply keep their click and keyboard behavior', () => {
  const quote = quoteElement(), opened = [];
  bindReplyNavigation(quote, 7, id => opened.push(id));
  const link = {closest: () => ({tagName: 'A'})};
  quote.onclick({target: link});
  quote.onkeydown({target: link, key: 'Enter', preventDefault() { assert.fail('Link intercepted'); }});
  assert.deepEqual(opened, []);
});

test('unavailable or invalid reply ids remain ordinary non-interactive quotes', () => {
  for (const id of [null, undefined, 0, -1, 1.5, 'invalid', Number.MAX_SAFE_INTEGER + 1]) {
    const quote = quoteElement();
    bindReplyNavigation(quote, id, () => assert.fail('Invalid reply opened'));
    assert.equal(quote.attributes.role, undefined);
    assert.equal(quote.onclick, undefined);
    assert.equal(quote.onkeydown, undefined);
  }
});

const app = await readFile(new URL('../app/inbox/static/app.js', import.meta.url), 'utf8');
const start = app.indexOf('async function focusMessage(');
const end = app.indexOf('\nfunction renderSearchResults(', start);
assert.ok(start >= 0 && end > start);

function focusContext(overrides = {}) {
  const calls = [];
  const context = vm.createContext({
    selected: 'a'.repeat(32), selectedMode: 'inbox', choosing: 1,
    messageFocusGeneration: 0, scopePath,
    focusLoadedMessage: id => { calls.push(['focus', id]); return false; },
    api: async () => ({message: {id: 7, text: 'Original'}}),
    renderMessages: (messages, key, options) => calls.push(['render', messages, key, options]),
    showError: (id, message) => calls.push(['error', id, message]),
    ...overrides,
  });
  vm.runInContext(app.slice(start, end), context);
  return {context, calls};
}

test('an unloaded original uses the exact endpoint in the selected Inbox or Library', async () => {
  for (const mode of ['inbox', 'library']) {
    const paths = [];
    const {context, calls} = focusContext({
      selectedMode: mode,
      api: async path => { paths.push(path); return {message: {id: 7, text: 'Original'}}; },
    });
    await context.focusMessage(7);
    assert.deepEqual(paths, [`${scopePath(mode, context.selected)}/messages/7`]);
    const rendered = calls.find(([action]) => action === 'render');
    assert.equal(rendered[1][0].id, 7);
    assert.equal(rendered[2], mode === 'library' ? `library:${context.selected}` : context.selected);
    assert.equal(rendered[3].merge, true);
    assert.equal(calls.filter(([action]) => action === 'focus').length, 2);
  }
});

test('a loaded reply original opens immediately without a request', async () => {
  const focused = [];
  const {context} = focusContext({
    api: async () => assert.fail('Loaded original fetched again'),
    focusLoadedMessage: id => { focused.push(id); return true; },
  });
  await context.focusMessage(7);
  assert.deepEqual(focused, [7]);
});

test('a delayed original cannot appear after switching chats, even away and back', async () => {
  for (const switchBack of [false, true]) {
    let resolve;
    const {context, calls} = focusContext({api: () => new Promise(done => { resolve = done; })});
    const loading = context.focusMessage(7);
    const originalKey = context.selected;
    context.selected = 'b'.repeat(32);
    context.choosing += 1;
    if (switchBack) { context.selected = originalKey; context.choosing += 1; }
    resolve({message: {id: 7, text: 'Original'}});
    await loading;
    assert.deepEqual(calls, [['focus', 7]]);
  }
});

test('an older pending reply cannot override a more recent loaded selection', async () => {
  let resolve;
  const {context, calls} = focusContext({
    api: () => new Promise(done => { resolve = done; }),
    focusLoadedMessage: id => { calls.push(['focus', id]); return id === 9; },
  });
  const first = context.focusMessage(7);
  await context.focusMessage(9);
  resolve({message: {id: 7, text: 'Original'}});
  await first;
  assert.deepEqual(calls.filter(([action]) => action === 'focus'), [['focus', 7], ['focus', 9]]);
  assert.ok(!calls.some(([action]) => action === 'render'));
});

test('missing originals show an error only while their chat is still selected', async () => {
  const {context, calls} = focusContext({api: async () => { throw new Error('Deleted'); }});
  await context.focusMessage(7);
  assert.ok(calls.some(([action, , message]) => action === 'error' && message === 'Deleted'));

  let reject;
  const stale = focusContext({api: () => new Promise((_, fail) => { reject = fail; })});
  const loading = stale.context.focusMessage(7);
  stale.context.choosing += 1;
  reject(new Error('Deleted'));
  await loading;
  assert.ok(!stale.calls.some(([action]) => action === 'error'));
});
