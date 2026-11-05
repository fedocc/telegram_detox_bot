import assert from 'node:assert/strict';
import test from 'node:test';
import {messageSummary, renderContent} from '../app/inbox/static/content.mjs';
import {renderableMessages} from '../app/inbox/static/ui.mjs';

const document = {createElement: tag => ({
  tag, children: [], attributes: {}, style: {}, textContent: '',
  setAttribute(key, value) { this.attributes[key] = value; },
  append(...nodes) { this.children.push(...nodes); },
  prepend(...nodes) { this.children.unshift(...nodes); },
})};
const all = node => [node, ...node.children.flatMap(all)];
const text = node => all(node).map(item => item.textContent).join(' ');
const poll = {kind: 'poll', title: 'Когда встречаемся?', anonymous: true,
  total_voters: 5, options: [{text: 'Во вторник', voters: 2, percent: 40, chosen: true},
    {text: 'В среду', voters: 3, percent: 60}]};

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
