import assert from 'node:assert/strict';
import test from 'node:test';
import {
  advanceOlderCursor,
  appendOnlyMessages,
  deepLinkFor,
  digestIdentity,
  digestTitle,
  dismissDigest,
  insertNewNodesInOrder,
  incrementalGrouping,
  isDigestDismissed,
  isMacNotifierClient,
  isTerminalConversationStatus,
  isWritable,
  mergeMessagePages,
  newPageMessages,
  moveSelectedSource,
  nextAuxiliaryPanel,
  normalizedSearchQuery,
  openedConversationIds,
  parseDeepLink,
  preferencePayload,
  retainFocusedMessage,
  renderableMessages,
  restoreScrollAnchor,
  scopePath,
  uploadWithinLimit,
  messagesChanged,
} from '../app/inbox/static/ui.mjs';

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
}

test('library pages merge in chronological order without duplicates', () => {
  assert.deepEqual(mergeMessagePages([{id:2}, {id:1}], [{id:2}, {id:3}]).map(x => x.id),
    [1, 2, 3]);
});

test('unchanged polling payload skips message reconciliation', () => {
  const messages=[{id:1,text:'one'},{id:2,text:'two'}];
  const signatures=new Map(messages.map(message=>[String(message.id),JSON.stringify(message)]));
  assert.equal(messagesChanged(signatures,messages),false);
  assert.equal(messagesChanged(signatures,[...messages,{id:3,text:'three'}]),true);
  assert.equal(messagesChanged(signatures,[{id:1,text:'changed'},messages[1]]),true);
});

test('reaction-only polling changes are meaningful without duplicating messages', () => {
  const original = [
    {id:1,text:'one',reactions:[{type:'emoji',emoji:'👍',count:1}]},
    {id:2,text:'two',reactions:[]},
  ];
  const signatures=new Map(original.map(message=>[String(message.id),JSON.stringify(message)]));
  const changed=[
    {...original[0],reactions:[{type:'emoji',emoji:'👍',count:2}]},
    original[1],
  ];
  assert.equal(messagesChanged(signatures,changed),true);
  assert.equal(signatures.get('2'),JSON.stringify(changed[1]));
  assert.equal(mergeMessagePages(changed,changed).length,2);
});

test('digest title and dismissal persist only for the current digest', () => {
  const storage = new MemoryStorage();
  const current = {period_end:'2026-09-20T04:00:00Z'};
  const next = {period_end:'2026-09-21T04:00:00Z'};
  assert.equal(digestTitle(current.period_end),'Сводка · 20 сент.');
  assert.equal(digestIdentity(current),`|${current.period_end}`);
  assert.equal(isDigestDismissed(storage,current),false);
  assert.equal(dismissDigest(storage,current),true);
  assert.equal(isDigestDismissed(storage,current),true);
  assert.equal(isDigestDismissed(storage,{...current}),true);
  assert.equal(isDigestDismissed(storage,next),false);
  assert.equal(isDigestDismissed(storage,{...current,id:2}),false);
});

test('append-only polling accepts new tail messages without hiding edits or removals', () => {
  const current=[{id:1,text:'one'},{id:2,text:'two'}];
  const signatures=new Map(current.map(message=>[String(message.id),JSON.stringify(message)]));
  assert.deepEqual(appendOnlyMessages(signatures,[...current,{id:3,text:'three'}]),
    [{id:3,text:'three'}]);
  assert.equal(appendOnlyMessages(signatures,[current[0],{id:2,text:'changed'},{id:3,text:'three'}]),null);
  assert.equal(appendOnlyMessages(signatures,[current[1],{id:3,text:'three'}]),null);
  assert.equal(appendOnlyMessages(signatures,[{id:0,text:'older'},...current]),null);
});

test('empty messages are omitted while service rows remain renderable', () => {
  assert.deepEqual(renderableMessages([
    {id:1,text:'',media:null,system:null},
    {id:2,text:'',media:null,system:'Участник присоединился'},
    {id:3,text:'hello',media:null,system:null},
  ]).map(message=>message.id),[2,3]);
});

test('captionless stickers and supported media are renderable while empty rows are not', () => {
  const media = kind => ({kind, url:`/${kind}`});
  assert.deepEqual(renderableMessages([
    {id:1,text:'',media:media('sticker'),system:null},
    {id:2,text:'',media:media('photo'),system:null},
    {id:3,text:'',media:media('video'),system:null},
    {id:4,text:'',media:media('voice'),system:null},
    {id:5,text:'',media:media('file'),system:null},
    {id:6,text:'',media:null,system:null},
  ]).map(message=>message.id),[1,2,3,4,5]);
});

test('Library and Notifications share one mutually exclusive panel state', () => {
  assert.equal(nextAuxiliaryPanel('none','library'),'library');
  assert.equal(nextAuxiliaryPanel('library','notifications'),'notifications');
  assert.equal(nextAuxiliaryPanel('notifications','library'),'library');
  assert.equal(nextAuxiliaryPanel('library','library'),'none');
  assert.equal(nextAuxiliaryPanel('notifications','notifications'),'none');
  assert.equal(nextAuxiliaryPanel('library','unknown'),'none');
});

test('prepend planner returns only unique nodes absent from existing history', () => {
  const existing=new Set(['51','52']);
  const page=[{id:50,text:'old'},{id:49,text:'older'},{id:50,text:'duplicate'},
    {id:51,text:'overlap'},{id:48,text:'',media:null,system:null}];
  assert.deepEqual(newPageMessages(existing,page).map(message=>message.id),[49,50]);
});

test('prepend insertion keeps existing node objects and inserts only missing nodes', () => {
  const oldA={id:'old-a'},oldB={id:'old-b'},newA={id:'new-a'},newB={id:'new-b'};
  const entries=new Map([
    ['1',{node:newA}],['2',{node:oldA}],['3',{node:newB}],['4',{node:oldB}],
  ]);
  const calls=[];
  insertNewNodesInOrder({insertBefore:(node,next)=>calls.push([node.id,next?.id || null])},
    ['1','2','3','4'],entries,new Set(['1','3']));
  assert.deepEqual(calls,[['new-b','old-b'],['new-a','old-a']]);
  assert.equal(entries.get('2').node,oldA);
  assert.equal(entries.get('4').node,oldB);
});

test('sticker pagination deduplicates and keeps existing message nodes', () => {
  const oldSticker={node:{id:'old-sticker'}};
  const entries=new Map([
    ['10',{node:{id:'new-sticker'}}],['11',oldSticker],
  ]), calls=[];
  insertNewNodesInOrder({insertBefore:(item,next)=>calls.push([item.id,next?.id || null])},
    ['10','11'],entries,new Set(['10']));
  assert.deepEqual(calls,[['new-sticker','old-sticker']]);
  assert.equal(entries.get('11'),oldSticker);
  const sticker={id:11,text:'',media:{kind:'sticker',sticker_format:'static'}};
  const signatures=new Map([['11',JSON.stringify(sticker)]]);
  assert.equal(messagesChanged(signatures,[sticker]),false);
  assert.deepEqual(newPageMessages(new Set(['11']),[sticker,sticker]),[]);
});

test('scroll restoration keeps the visual anchor offset exactly', () => {
  const list={scrollTop:120,scrollHeight:900};
  const entries=new Map([['anchor',{node:{getBoundingClientRect:()=>({top:73})}}]]);
  restoreScrollAnchor(list,entries,{id:'anchor',top:41},500);
  assert.equal(list.scrollTop,152);
  restoreScrollAnchor(list,entries,null,800);
  assert.equal(list.scrollTop,252);
});

test('incremental grouping updates inserted messages and the existing page boundary only', () => {
  const messages=[
    {id:1,timestamp:'2026-09-18T23:59:00Z',sender:'Ada',own:false},
    {id:2,timestamp:'2026-09-19T08:00:00Z',sender:'Ada',own:false},
    {id:3,timestamp:'2026-09-19T08:01:00Z',sender:'Ada',own:false},
    {id:4,timestamp:'2026-09-19T08:02:00Z',sender:'Ada',own:false},
  ];
  assert.deepEqual(incrementalGrouping(messages,new Set(['1','2'])),[
    ['1',false],['2',false],['3',true],
  ]);
  assert.deepEqual(incrementalGrouping([
    messages[1],{...messages[2],system:'Ada закрепила сообщение'},messages[3],
  ],new Set(['3'])),[['3',false],['4',false]]);
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
