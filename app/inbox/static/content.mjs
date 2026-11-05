export function messageSummary(message) {
  if (message.system || message.text) return message.system || message.text;
  const content = message.content;
  if (content?.kind === 'poll') return `${content.quiz ? 'Викторина' : 'Опрос'}: ${content.title}`;
  return content?.title || message.media?.name || 'Вложение';
}

function safeWebUrl(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}

export function renderContent(content, document = globalThis.document) {
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const card = node('section', `message-content content-${content.kind}`);
  card.setAttribute('aria-label', content.kind === 'poll' ? 'Опрос' : content.label || 'Вложение');
  card.append(node('div', 'content-title', content.title));
  if (content.kind === 'poll') {
    const description = content.quiz ? 'Викторина'
      : content.anonymous ? 'Анонимный опрос' : 'Открытый опрос';
    card.append(node('div', 'content-note', description
      + (content.multiple ? ' · Несколько вариантов' : '')));
    const options = node('ul', 'poll-options');
    for (const option of content.options || []) {
      const item = node('li', 'poll-option' + (option.chosen ? ' poll-chosen' : '')
        + (option.correct ? ' poll-correct' : ''));
      const heading = node('div', 'poll-option-heading');
      heading.append(node('span', 'poll-option-text', option.text));
      if (Number.isFinite(option.percent)) {
        heading.append(node('span', 'poll-percent', `${option.percent}%`));
      }
      item.append(heading);
      const notes = [];
      if (option.chosen) notes.push('Ваш ответ');
      if (option.correct) notes.push('Правильный ответ');
      if (Number.isSafeInteger(option.voters)) notes.push(`Голосов: ${option.voters}`);
      if (notes.length) item.append(node('div', 'content-note', notes.join(' · ')));
      if (Number.isFinite(option.percent)) {
        const rail = node('div', 'poll-rail'), bar = node('span', 'poll-bar');
        rail.setAttribute('aria-hidden', 'true');
        bar.style.width = `${Math.max(0, Math.min(100, option.percent))}%`;
        rail.append(bar); item.append(rail);
      }
      options.append(item);
    }
    card.append(options);
    const footer = [];
    if (content.closed) footer.push(content.quiz ? 'Викторина завершена' : 'Опрос завершён');
    if (Number.isSafeInteger(content.total_voters)) {
      footer.push(content.total_voters === 0 ? 'Пока никто не проголосовал'
        : `Проголосовали: ${content.total_voters}`);
    }
    if (footer.length) card.append(node('div', 'content-note poll-footer', footer.join(' · ')));
    if (content.solution) card.append(node('p', 'poll-solution', content.solution));
    return card;
  }
  if (content.label && content.label !== content.title) {
    card.prepend(node('div', 'content-note', content.label));
  }
  if (content.text) card.append(node('p', 'content-text', content.text));
  let url = content.kind === 'webpage' ? safeWebUrl(content.url) : null;
  if (content.kind === 'location' && Number.isFinite(content.latitude)
      && Number.isFinite(content.longitude) && Math.abs(content.latitude) <= 90
      && Math.abs(content.longitude) <= 180) {
    card.append(node('div', 'content-note', `${content.latitude.toFixed(5)}, ${content.longitude.toFixed(5)}`));
    url = `https://www.openstreetmap.org/?mlat=${content.latitude}&mlon=${content.longitude}#map=16/${content.latitude}/${content.longitude}`;
  }
  if (url) {
    const link = node('a', 'content-link', content.kind === 'location' ? 'Открыть карту' : 'Открыть ссылку');
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    card.append(link);
  }
  return card;
}
