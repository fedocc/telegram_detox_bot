import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createNotificationToggle, linkedConversation, notificationMode,
} from '../app/inbox/static/notifications.mjs';

function fixture() {
  const renders = [], calls = [];
  let state = {enabled:true, effective_enabled:true, mute_until:null, permission:'authorized'};
  let fail = false;
  const toggle = createNotificationToggle({
    render: value => renders.push(value),
    request: async (path, token, body) => {
      calls.push([path, token, body]);
      if (fail) throw new Error('offline');
      if (path !== '/status') {
        assert.equal(token, 'token');
        if (path === '/enable') state={...state,enabled:true,effective_enabled:true,mute_until:null};
        if (path === '/disable') state={...state,enabled:false,effective_enabled:false,mute_until:null};
        if (path === '/snooze') state={...state,enabled:true,effective_enabled:false,mute_until:2000};
      }
      return {...state, csrf:'token'};
    },
  });
  return {toggle, calls, renders, fail() { fail = true; }};
}

test('status then permanent OFF then ON uses only bridge state', async () => {
  const f = fixture();
  await f.toggle.refresh(); assert.equal(f.renders.at(-1).effective_enabled, true);
  await f.toggle.toggle(); assert.equal(f.renders.at(-1).enabled, false);
  await f.toggle.toggle(); assert.equal(f.renders.at(-1).effective_enabled, true);
  assert.deepEqual(f.calls.map(x => x[0]), ['/status', '/disable', '/enable']);
  assert.deepEqual(f.calls.slice(1).map(x => x[2]), [{}, {}]);
});

test('notification modes map exactly to ON, PAUSE and permanent OFF semantics', () => {
  assert.equal(notificationMode({enabled:true, effective_enabled:true, mute_until:null}), 'enable');
  assert.equal(notificationMode({enabled:true, effective_enabled:false, mute_until:2000}), 'pause');
  assert.equal(notificationMode({enabled:false, effective_enabled:false, mute_until:null}), 'disable');
});

test('all snooze durations use an exact numeric JSON body and Enable Now clears pause', async () => {
  const f = fixture(); await f.toggle.refresh();
  for (const seconds of [600, 1800, 3600, 10800, 21600, 43200]) {
    await f.toggle.action(String(seconds));
    assert.equal(f.calls.at(-1)[0], '/snooze');
    assert.deepEqual(f.calls.at(-1)[2], {seconds});
    assert.equal(f.renders.at(-1).effective_enabled, false);
  }
  await f.toggle.action('enable');
  assert.equal(f.calls.at(-1)[0], '/enable');
  assert.equal(f.renders.at(-1).effective_enabled, true);
  assert.equal(f.renders.at(-1).mute_until, null);
});

test('open notifies the local bridge with only a validated conversation id', async () => {
  const f = fixture(), id='a'.repeat(32); await f.toggle.refresh();
  await f.toggle.conversationOpened(id);
  await f.toggle.conversationOpened('bad');
  assert.deepEqual(f.calls.at(-1), ['/conversation-opened', 'token', {conversation_id:id}]);
  assert.equal(f.calls.length, 2);
});

test('unknown actions are ignored client-side', async () => {
  const f = fixture(); await f.toggle.refresh();
  await f.toggle.action('60'); await f.toggle.action('NaN');
  assert.equal(f.calls.length, 1);
});

test('unavailable bridge disables controls without throwing or opening writes', async () => {
  const f = fixture(); f.fail(); await f.toggle.refresh();
  await f.toggle.toggle(); await f.toggle.conversationOpened('a'.repeat(32));
  assert.equal(f.renders.at(-1).available, false); assert.equal(f.calls.length, 1);
});

test('failed action preserves prior state and shows inline error', async () => {
  const f = fixture(); await f.toggle.refresh(); f.fail(); await f.toggle.toggle();
  assert.equal(f.renders.at(-2).enabled, true);
  assert.equal(f.renders.at(-1).enabled, true);
  assert.equal(f.renders.at(-1).error, 'Не удалось изменить');
});

test('a double click while a request is pending issues one write', async () => {
  let finish, writes = 0;
  const toggle = createNotificationToggle({render() {}, request: async (path) => {
    if (path === '/status') return {enabled:true,effective_enabled:true,csrf:'token'};
    writes++; return new Promise(resolve => { finish = resolve; });
  }});
  await toggle.refresh(); const action = toggle.toggle(); await toggle.toggle();
  assert.equal(writes, 1); finish({enabled:false,effective_enabled:false,csrf:'token'}); await action;
});

test('click link selects only a visible valid conversation; closed or invalid falls back', () => {
  const id = 'a'.repeat(32), rows = [{id}];
  assert.equal(linkedConversation(`?conversation=${id}`, rows), id);
  assert.equal(linkedConversation(`?conversation=${id}`, []), null);
  assert.equal(linkedConversation('?conversation=https://evil.test', rows), null);
  assert.equal(linkedConversation('', rows), null);
});
