import test from 'node:test';
import assert from 'node:assert/strict';
import {createNotificationToggle, linkedConversation} from '../app/inbox/static/notifications.mjs';

function fixture() {
  const renders = [], calls = [];
  let enabled = true, fail = false;
  const toggle = createNotificationToggle({
    render: value => renders.push(value),
    request: async (path, token) => {
      calls.push([path, token]);
      if (fail) throw new Error('offline');
      if (path !== '/status') { assert.equal(token, 'token'); enabled = path === '/enable'; }
      return {enabled, csrf: 'token', permission: 'authorized'};
    },
  });
  return {toggle, calls, renders, fail() { fail = true; }};
}
test('status then ON -> OFF -> ON; only bridge calls, no lifecycle actions', async () => {
  const f = fixture();
  await f.toggle.refresh(); assert.equal(f.renders.at(-1).enabled, true);
  await f.toggle.toggle(); assert.equal(f.renders.at(-1).enabled, false);
  await f.toggle.toggle(); assert.equal(f.renders.at(-1).enabled, true);
  assert.deepEqual(f.calls.map(x => x[0]), ['/status', '/disable', '/enable']);
});
test('unavailable bridge disables switch without throwing', async () => {
  const f = fixture(); f.fail(); await f.toggle.refresh(); await f.toggle.toggle();
  assert.equal(f.renders.at(-1).available, false); assert.equal(f.calls.length, 1);
});
test('failed toggle rolls back optimistic value and shows inline error', async () => {
  const f = fixture(); await f.toggle.refresh(); f.fail(); await f.toggle.toggle();
  assert.equal(f.renders.at(-2).enabled, false);
  assert.equal(f.renders.at(-1).enabled, true);
  assert.equal(f.renders.at(-1).error, 'Не удалось изменить');
});
test('a double click while request is pending does not issue duplicate writes', async () => {
  let finish, writes = 0;
  const toggle = createNotificationToggle({render() {}, request: async (path) => {
    if (path === '/status') return {enabled: true, csrf: 'token'};
    writes++; return new Promise(resolve => { finish = resolve; });
  }});
  await toggle.refresh(); const action = toggle.toggle(); await toggle.toggle();
  assert.equal(writes, 1); finish({enabled:false}); await action;
});
test('click link selects only a visible valid conversation; closed/invalid falls back', () => {
  const id = 'a'.repeat(32), rows = [{id}];
  assert.equal(linkedConversation(`?conversation=${id}`, rows), id);
  assert.equal(linkedConversation(`?conversation=${id}`, []), null);
  assert.equal(linkedConversation('?conversation=https://evil.test', rows), null);
  assert.equal(linkedConversation('', rows), null);
});
