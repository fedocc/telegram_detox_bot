import assert from 'node:assert/strict';
import test from 'node:test';
import {
  advanceOlderCursor,
  deepLinkFor,
  isMacNotifierClient,
  isTerminalConversationStatus,
  isWritable,
  mergeMessagePages,
  moveSelectedSource,
  normalizedSearchQuery,
  openedConversationIds,
  parseDeepLink,
  preferencePayload,
  retainFocusedMessage,
  scopePath,
  uploadWithinLimit,
} from '../app/inbox/static/ui.mjs';

test('library pages merge in chronological order without duplicates', () => {
  assert.deepEqual(mergeMessagePages([{id:2}, {id:1}], [{id:2}, {id:3}]).map(x => x.id),
    [1, 2, 3]);
});

test('three 50-message history pages merge without gaps or duplicates', () => {
  const page = (first, last) => Array.from({length:last - first + 1}, (_, index) => ({
    id:first + index,
  }));
  let messages = mergeMessagePages([], page(101, 150));
  messages = mergeMessagePages(page(51, 100), messages);
  messages = mergeMessagePages(page(1, 50), messages);
  assert.equal(messages.length, 150);
  assert.deepEqual(messages.map(value => value.id), page(1, 150).map(value => value.id));
});

test('an exact far-away Inbox message survives a normal latest-page refresh', () => {
  const latest = Array.from({length:50}, (_, index) => ({id:101 + index}));
  const focused = {id:7, text:'pinned far outside the latest page'};
  const kept = retainFocusedMessage(latest, 7, focused);
  assert.equal(kept.length, 51);
  assert.equal(kept[0], focused);
  assert.deepEqual(retainFocusedMessage(latest, null, focused), latest);
  assert.equal(retainFocusedMessage(latest, 101, latest[0]), latest);
});

test('Library open exposes only validated unique Inbox projection ids to the bridge', () => {
  const first = 'a'.repeat(32), second = 'b'.repeat(32);
  assert.deepEqual(openedConversationIds({
    opened_conversation_ids:[first, 'bad', first, second, 12, 'A'.repeat(32)],
  }), [first, second]);
  assert.deepEqual(openedConversationIds({opened_conversation_ids:'crafted'}), []);
  assert.deepEqual(openedConversationIds(null), []);
});

test('only active conversations and Saved Messages are writable', () => {
  assert.equal(isWritable('inbox'), true);
  assert.equal(isWritable('library', {writable:true}), true);
  assert.equal(isWritable('library', {writable:false}), false);
});

test('upload limit rejects empty and oversized files', () => {
  assert.equal(uploadWithinLimit({size:1}, 100), true);
  assert.equal(uploadWithinLimit({size:0}, 100), false);
  assert.equal(uploadWithinLimit({size:101}, 100), false);
});

test('older cursor advances strictly backwards and stops on invalid/same cursors', () => {
  assert.equal(advanceOlderCursor(null, 151), 151);
  assert.equal(advanceOlderCursor(151, 101), 101);
  assert.equal(advanceOlderCursor(101, 101), null);
  assert.equal(advanceOlderCursor(101, 120), null);
  assert.equal(advanceOlderCursor(51, null), null);
});

test('deep links resolve only currently allowed conversations and library sources', () => {
  const id = 'a'.repeat(32);
  const conversations = [{id}];
  const sources = [{id:'saved'}, {id:'s-course'}];
  assert.deepEqual(parseDeepLink(`?conversation=${id}`, conversations, sources),
    {mode:'inbox', id, messageId:null});
  assert.deepEqual(parseDeepLink('?library=s-course&message=42', conversations, sources),
    {mode:'library', id:'s-course', messageId:42});
  assert.equal(parseDeepLink('?library=crafted&message=42', conversations, sources), null);
  assert.equal(parseDeepLink('?conversation=evil', conversations, sources), null);
  assert.equal(deepLinkFor('library', 's-course', 42), '/?library=s-course&message=42');
  assert.equal(deepLinkFor('inbox', id), `/?conversation=${id}`);
});

test('source endpoints never accept a peer id from a payload helper', () => {
  assert.equal(scopePath('library', 's-course'), '/api/library/s-course');
  assert.equal(scopePath('inbox', 'a'.repeat(32)), `/api/conversations/${'a'.repeat(32)}`);
  assert.equal(scopePath('other', '123'), '');
  assert.equal(isTerminalConversationStatus(404), true);
  assert.equal(isTerminalConversationStatus(410), true);
  assert.equal(isTerminalConversationStatus(503), false);
});

test('search requires three normalized characters', () => {
  assert.equal(normalizedSearchQuery(' a '), '');
  assert.equal(normalizedSearchQuery('  deep   learning  '), 'deep learning');
});

test('Mac notifier controls are hidden on iPhone, iPad and non-Mac clients', () => {
  assert.equal(isMacNotifierClient({platform:'MacIntel', userAgent:'Macintosh', maxTouchPoints:0}), true);
  assert.equal(isMacNotifierClient({platform:'iPhone', userAgent:'iPhone', maxTouchPoints:5}), false);
  assert.equal(isMacNotifierClient({platform:'MacIntel', userAgent:'Macintosh', maxTouchPoints:5}), false);
  assert.equal(isMacNotifierClient({platform:'Linux', userAgent:'Android', maxTouchPoints:5}), false);
});

test('management preference payload is bounded and bot write is bot-only', () => {
  assert.deepEqual(preferencePayload({
    token:'opaque', selected:true, notifications_muted:true, allow_bot_write:true,
    digest_excluded:false, is_bot:false,
  }), {
    token:'opaque', library_enabled:true, notifications_muted:true,
    allow_bot_write:false, digest_excluded:false,
  });
  assert.deepEqual(preferencePayload({
    token:'stale-token', source_id:'s-bot', selected:true, notifications_muted:false,
    allow_bot_write:true, digest_excluded:true, is_bot:true,
  }), {
    source_id:'s-bot', library_enabled:true, notifications_muted:false,
    allow_bot_write:true, digest_excluded:true,
  });
});

test('selected source ordering changes without including unselected dialogs', () => {
  const rows = [
    {source_id:'saved', selected:true}, {source_id:'a', selected:true},
    {source_id:null, selected:false}, {source_id:'b', selected:true},
  ];
  assert.deepEqual(moveSelectedSource(rows, 'b', -1), ['saved', 'b', 'a']);
  assert.deepEqual(moveSelectedSource(rows, 'saved', -1), ['saved', 'a', 'b']);
});
