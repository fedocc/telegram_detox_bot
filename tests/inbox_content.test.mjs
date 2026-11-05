import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import {messageSummary, renderContent} from '../app/inbox/static/content.mjs';
import {renderableMessages, scopePath} from '../app/inbox/static/ui.mjs';

const document = {createElement: tag => ({
  tag, children: [], attributes: {}, style: {}, textContent: '',
  setAttribute(key, value) { this.attributes[key] = value; },
  append(...nodes) { this.children.push(...nodes); },
  prepend(...nodes) { this.children.unshift(...nodes); },
})};
const all = node => [node, ...node.children.flatMap(all)];
const text = node => all(node).map(item => item.textContent).join(' ');
const poll = {kind: 'poll', title: 'Когда встречаемся?', anonymous: true,
  id: '9223372036854775800', voted: true, can_revote: true,
  total_voters: 5, options: [{id: 'a', text: 'Во вторник', voters: 2, percent: 40, chosen: true},
    {id: 'b', text: 'В среду', voters: 3, percent: 60}]};

test('content-only messages survive the history filter without keeping empty rows', () => {
  const messages = [{id: 1}, {id: 2, content: poll}, {id: 3, content: {kind: 'unsupported'}}];
  assert.deepEqual(renderableMessages(messages).map(item => item.id), [2, 3]);
});

test('polls show question, choices, available results and own vote as non-writing controls', () => {
  const card = renderContent(poll, document);
  assert.match(text(card), /Когда встречаемся\?/);
  assert.match(text(card), /Во вторник.*40%.*Ваш ответ.*Голосов: 2/);
  assert.match(text(card), /Проголосовали: 5/);
  assert.deepEqual(all(card).filter(item => item.className === 'poll-bar').map(item => item.style.width),
    ['40%', '60%']);
  assert.ok(!all(card).some(item => ['button', 'input', 'iframe', 'img'].includes(item.tag)));
});

test('unanswered polls never invent zero percentages or voters', () => {
  const card = renderContent({...poll, total_voters: 0, options: [{text: 'Да', percent: null}]}, document);
  assert.doesNotMatch(text(card), /0%|Голосов:/);
  assert.match(text(card), /Пока никто не проголосовал/);
});

test('quiz explanation and closed state remain readable', () => {
  const card = renderContent({...poll, quiz: true, closed: true, solution: 'Объяснение'}, document);
  assert.match(text(card), /Викторина завершена/);
  assert.match(text(card), /Объяснение/);
});

test('message menus, pins and search use poll titles instead of a blank attachment', () => {
  assert.equal(messageSummary({content: poll}), 'Опрос: Когда встречаемся?');
  assert.equal(messageSummary({content: {...poll, quiz: true}}), 'Викторина: Когда встречаемся?');
  assert.equal(messageSummary({text: 'Подпись', content: poll}), 'Подпись');
  assert.equal(messageSummary({media: {name: 'Файл.pdf'}}), 'Файл.pdf');
});

test('untrusted preview text is never parsed as HTML and unsafe links are not created', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,boom', '//evil.test']) {
    const card = renderContent({kind: 'webpage', title: '<img src=x onerror=alert(1)>',
      text: '<script>boom</script>', url}, document);
    assert.match(text(card), /<img src=x/);
    assert.ok(!all(card).some(item => ['a', 'img', 'script'].includes(item.tag)));
  }
  const card = renderContent({kind: 'webpage', title: 'Заметка', url: 'https://example.org'}, document);
  const link = all(card).find(item => item.tag === 'a');
  assert.equal(link.rel, 'noopener noreferrer');
  assert.equal(link.href, 'https://example.org/');
});

test('locations load no map automatically and link only valid coordinates', () => {
  const card = renderContent({kind: 'location', title: 'Место', latitude: 55.75, longitude: 37.62}, document);
  assert.equal(all(card).filter(item => item.tag === 'a').length, 1);
  assert.ok(!all(card).some(item => ['img', 'iframe'].includes(item.tag)));
  const invalid = renderContent({kind: 'location', latitude: 91, longitude: 0}, document);
  assert.ok(!all(invalid).some(item => item.tag === 'a'));
});

test('single-choice voting submits once and disables controls until completion', async () => {
  let finish;
  const votes = [];
  const card = renderContent(poll, document, {onVote: options => {
    votes.push(options); return new Promise(resolve => { finish = resolve; });
  }});
  const buttons = all(card).filter(item => item.className === 'poll-choice');
  const pending = buttons[1].onclick();
  assert.deepEqual(votes, [['b']]);
  assert.ok(buttons.every(item => item.disabled));
  await buttons[0].onclick();
  assert.equal(votes.length, 1);
  assert.match(text(card), /Отправляю ответ/);
  finish(); await pending;
  assert.ok(buttons.every(item => !item.disabled));
});

test('multiple-choice draft persists across refreshes and requires explicit submission', async () => {
  const votes = [], draft = {};
  const data = {...poll, multiple: true, voted: false,
    options: poll.options.map(item => ({...item, chosen: false}))};
  let card = renderContent(data, document, {draft, onVote: options => votes.push(options)});
  const choices = all(card).filter(item => item.className === 'poll-choice');
  choices[0].onclick(); choices[1].onclick();
  assert.deepEqual(votes, []);
  assert.deepEqual(draft.selected, ['a', 'b']);
  card = renderContent({...data, total_voters: 6}, document,
    {draft, onVote: options => votes.push(options)});
  assert.ok(all(card).filter(item => item.className === 'poll-choice')
    .every(item => item.attributes['aria-pressed'] === 'true'));
  await all(card).find(item => item.className === 'poll-submit').onclick();
  assert.deepEqual(votes, [['a', 'b']]);
});

test('an empty multiple-choice selection cannot be submitted and errors preserve choices', async () => {
  const draft = {}, card = renderContent({...poll, voted: false, multiple: true,
    options: poll.options.map(item => ({...item, chosen: false}))}, document, {
    draft, onVote: () => { throw new Error('Опрос завершён'); },
  });
  const submit = all(card).find(item => item.className === 'poll-submit');
  assert.ok(submit.disabled);
  all(card).find(item => item.className === 'poll-choice').onclick();
  assert.equal(submit.disabled, false);
  await submit.onclick();
  assert.match(text(card), /Опрос завершён/);
  assert.deepEqual(draft.selected, ['a']);
  assert.equal(submit.disabled, false);
});

test('vote can be retracted, while closed, locked quizzes and digest cards remain read-only', async () => {
  const votes = [], card = renderContent(poll, document, {onVote: options => votes.push(options)});
  await all(card).find(item => item.className === 'poll-retract').onclick();
  assert.deepEqual(votes, [[]]);
  for (const data of [{...poll, closed: true}, {...poll, quiz: true, can_revote: false},
    {...poll, can_revote: false}]) {
    const locked = renderContent(data, document, {onVote: () => assert.fail('Locked poll voted')});
    assert.ok(!all(locked).some(item => item.tag === 'button'));
  }
  assert.ok(!all(renderContent(poll, document)).some(item => item.tag === 'button'));
});

const app = await readFile(new URL('../app/inbox/static/app.js', import.meta.url), 'utf8');
const votingSource = app.slice(app.indexOf('function pollVoting('), app.indexOf('\nfunction messageNode('));
function votingContext(overrides = {}) {
  const calls = [], message = {id: 100, text: 'Подпись', content: poll, reactions: [{emoji: '👍'}]};
  const context = vm.createContext({
    selected: 'a'.repeat(32), selectedMode: 'inbox', choosing: 1, scopePath,
    pollDrafts: new Map(), messageNodes: new Map([['100', {message}]]),
    api: async (path, body) => { calls.push(['api', path, body]);
      return {content: {...poll, voted: true}}; },
    renderMessages: (...args) => calls.push(['render', ...args]), ...overrides,
  });
  vm.runInContext(votingSource, context);
  return {context, calls, message};
}

test('vote updates the exact Inbox or Library message without dropping other message fields', async () => {
  for (const mode of ['inbox', 'library']) {
    const {context, calls, message} = votingContext({selectedMode: mode});
    await context.pollVoting(message).onVote(['b']);
    assert.equal(calls[0][1], `${scopePath(mode, context.selected)}/messages/100/vote`);
    assert.equal(calls[0][2].poll_id, '9223372036854775800');
    const rendered = calls[1];
    assert.equal(rendered[1][0].text, 'Подпись');
    assert.equal(rendered[1][0].reactions[0].emoji, '👍');
    assert.equal(rendered[2], mode === 'library' ? `library:${context.selected}` : context.selected);
    assert.equal(rendered[3].merge, true);
  }
});

test('switching chats prevents a stale poll from sending or rendering into another chat', async () => {
  const stale = votingContext(), action = stale.context.pollVoting(stale.message);
  stale.context.choosing++;
  await assert.rejects(action.onVote(['a']), /Откройте этот чат/);
  assert.deepEqual(stale.calls, []);
  let finish;
  const pending = votingContext({api: () => new Promise(resolve => { finish = resolve; })});
  const sending = pending.context.pollVoting(pending.message).onVote(['b']);
  pending.context.selected = 'b'.repeat(32); pending.context.choosing++;
  finish({content: poll}); await sending;
  assert.deepEqual(pending.calls, []);
});
