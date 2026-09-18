export function mergeMessagePages(older, current) {
  return [...new Map([...older, ...current].map(message => [message.id, message]))
    .values()].sort((a, b) => a.id - b.id);
}

export function retainFocusedMessage(messages, focusedId, focusedMessage) {
  const page = Array.isArray(messages) ? messages : [];
  const id = Number(focusedId);
  if (!Number.isSafeInteger(id) || id <= 0 || !focusedMessage
      || Number(focusedMessage.id) !== id || page.some(message => Number(message.id) === id)) {
    return page;
  }
  return mergeMessagePages([focusedMessage], page);
}

export function openedConversationIds(result) {
  if (!Array.isArray(result?.opened_conversation_ids)) return [];
  return [...new Set(result.opened_conversation_ids.filter(
    value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value),
  ))];
}

export function isWritable(mode, source) {
  return mode === 'inbox' || (mode === 'library' && source?.writable === true);
}

export function uploadWithinLimit(file, maxBytes) {
  return Boolean(file && Number.isSafeInteger(file.size) && file.size > 0
    && file.size <= maxBytes);
}

export function scopePath(mode, id) {
  const encoded = encodeURIComponent(String(id || ''));
  if (mode === 'inbox') return `/api/conversations/${encoded}`;
  if (mode === 'library') return `/api/library/${encoded}`;
  return '';
}

export function isTerminalConversationStatus(status) {
  return status === 404 || status === 410;
}

export function normalizedSearchQuery(value) {
  const query = String(value || '').trim().replace(/\s+/g, ' ');
  return query.length >= 3 ? query : '';
}

export function advanceOlderCursor(requested, returned) {
  if (returned === null || returned === undefined || returned === '') return null;
  const next = Number(returned);
  if (!Number.isSafeInteger(next) || next <= 0) return null;
  if (requested !== null && requested !== undefined) {
    const previous = Number(requested);
    if (!Number.isSafeInteger(previous) || next >= previous) return null;
  }
  return next;
}

export function parseDeepLink(search, conversations, sources) {
  const params = new URLSearchParams(search || '');
  const messageValue = params.get('message');
  const numericMessage = messageValue && /^[1-9][0-9]*$/.test(messageValue)
    ? Number(messageValue) : null;
  const messageId = Number.isSafeInteger(numericMessage) ? numericMessage : null;
  const conversation = params.get('conversation');
  if (/^[a-f0-9]{32}$/.test(conversation || '')
      && conversations.some(row => row.id === conversation)) {
    return {mode: 'inbox', id: conversation, messageId};
  }
  const source = params.get('library');
  if (source && sources.some(row => row.id === source)) {
    return {mode: 'library', id: source, messageId};
  }
  return null;
}

export function deepLinkFor(mode, id, messageId = null) {
  const params = new URLSearchParams();
  if (mode === 'inbox') params.set('conversation', id);
  if (mode === 'library') params.set('library', id);
  if (messageId && Number.isSafeInteger(Number(messageId)) && Number(messageId) > 0) {
    params.set('message', String(messageId));
  }
  const query = params.toString();
  return query ? `/?${query}` : '/';
}

export function isMacNotifierClient({platform = '', userAgent = '', maxTouchPoints = 0} = {}) {
  const looksMac = /mac/i.test(platform) || /macintosh/i.test(userAgent);
  const looksMobile = /iphone|ipad|ipod|android/i.test(userAgent)
    || (/mac/i.test(platform) && Number(maxTouchPoints) > 1);
  return looksMac && !looksMobile;
}

export function preferencePayload(row) {
  const result = {
    library_enabled: Boolean(row.selected),
    notifications_muted: Boolean(row.notifications_muted),
    allow_bot_write: Boolean(row.is_bot && row.allow_bot_write),
    digest_excluded: Boolean(row.digest_excluded),
  };
  if (row.source_id) result.source_id = row.source_id;
  else if (row.token) result.token = row.token;
  return result;
}

export function moveSelectedSource(rows, sourceId, direction) {
  const selected = rows.filter(row => row.selected && row.source_id);
  const index = selected.findIndex(row => row.source_id === sourceId);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= selected.length) {
    return selected.map(row => row.source_id);
  }
  [selected[index], selected[target]] = [selected[target], selected[index]];
  return selected.map(row => row.source_id);
}
