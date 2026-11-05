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

export function renderContent(content, document = globalThis.document, {onVote = null, draft = {}} = {}) {
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
    const editable = typeof onVote === 'function' && !content.closed
      && (!content.voted || content.can_revote);
    const current = new Set(content.options.filter(option => option.chosen).map(option => option.id));
    const valid = new Set(content.options.map(option => option.id));
    const selected = new Set((draft.selected || [...current]).filter(id => valid.has(id)));
    const choices = [], controls = [];
    let pending = false, submit = null;
    const status = node('div', 'content-note poll-status'); status.setAttribute('role', 'status');
    const sync = () => {
      for (const {button, mark, option} of choices) {
        button.disabled = pending;
        button.setAttribute('aria-pressed', String(selected.has(option.id)));
        mark.textContent = selected.has(option.id) ? '✓' : '';
      }
      for (const control of controls) control.disabled = pending;
      if (submit) submit.disabled = pending || !selected.size
        || (selected.size === current.size && [...selected].every(id => current.has(id)));
    };
    const send = async options => {
      if (pending) return;
      pending = true; status.textContent = 'Отправляю ответ…'; sync();
      try {
        await onVote(options);
        status.textContent = '';
      } catch (error) { status.textContent = error.message || 'Не удалось отправить ответ.'; }
      finally { pending = false; sync(); }
    };
    const options = node('ul', 'poll-options');
    for (const option of content.options || []) {
      const item = node('li', 'poll-option' + (option.chosen ? ' poll-chosen' : '')
        + (option.correct ? ' poll-correct' : ''));
      const body = node(editable ? 'button' : 'div', editable ? 'poll-choice' : '');
      if (editable) {
        body.type = 'button';
        body.setAttribute('aria-label', `Выбрать: ${option.text}`);
        const mark = node('span', 'poll-choice-mark' + (content.multiple ? ' multiple' : ''));
        mark.setAttribute('aria-hidden', 'true'); body.append(mark);
        choices.push({button: body, mark, option});
        body.onclick = () => {
          if (pending) return;
          if (!content.multiple) return send([option.id]);
          if (selected.has(option.id)) selected.delete(option.id); else selected.add(option.id);
          draft.selected = [...selected]; sync();
        };
      }
      const copy = node('div', 'poll-option-copy'), heading = node('div', 'poll-option-heading');
      heading.append(node('span', 'poll-option-text', option.text));
      if (Number.isFinite(option.percent)) {
        heading.append(node('span', 'poll-percent', `${option.percent}%`));
      }
      copy.append(heading);
      const notes = [];
      if (option.chosen) notes.push('Ваш ответ');
      if (option.correct) notes.push('Правильный ответ');
      if (Number.isSafeInteger(option.voters)) notes.push(`Голосов: ${option.voters}`);
      if (notes.length) copy.append(node('div', 'content-note', notes.join(' · ')));
      if (Number.isFinite(option.percent)) {
        const rail = node('div', 'poll-rail'), bar = node('span', 'poll-bar');
        rail.setAttribute('aria-hidden', 'true');
        bar.style.width = `${Math.max(0, Math.min(100, option.percent))}%`;
        rail.append(bar); copy.append(rail);
      }
      body.append(copy); item.append(body);
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
    if (editable) {
      const actions = node('div', 'poll-actions');
      if (content.multiple) {
        submit = node('button', 'poll-submit', content.voted ? 'Изменить ответ' : 'Проголосовать');
        submit.type = 'button'; submit.onclick = () => send([...selected]); actions.append(submit);
      }
      if (content.voted && content.can_revote) {
        const retract = node('button', 'poll-retract', 'Отменить голос');
        retract.type = 'button'; retract.onclick = () => send([]);
        controls.push(retract); actions.append(retract);
      }
      card.append(actions, status); sync();
    }
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
