export function mergeMessagePages(older, current) {
  return [...new Map([...older, ...current].map(message => [message.id, message]))
    .values()].sort((a, b) => a.id - b.id);
}

export function renderableMessages(messages) {
  return (Array.isArray(messages) ? messages : []).filter(message => message
    && (typeof message.system === 'string' && message.system.trim()
      || typeof message.text === 'string' && message.text.length
      || message.media));
}

export function newPageMessages(existingIds, messages) {
  const known = existingIds instanceof Set ? existingIds : new Set();
  return [...new Map(renderableMessages(messages).map(message => [String(message.id), message]))
    .entries()].filter(([id]) => !known.has(id)).map(([, message]) => message)
    .sort((a, b) => a.id - b.id);
}

export function insertNewNodesInOrder(list, orderedIds, entries, insertedIds) {
  let next = null;
  for (let index = orderedIds.length - 1; index >= 0; index -= 1) {
    const id = orderedIds[index], element = entries.get(id).node;
    if (insertedIds.has(id)) list.insertBefore(element, next);
    next = element;
  }
}

export function restoreScrollAnchor(list, entries, anchor, oldHeight) {
  const anchored = anchor && entries.get(anchor.id)?.node;
  if (anchored) list.scrollTop += anchored.getBoundingClientRect().top - anchor.top;
  else list.scrollTop += list.scrollHeight - oldHeight;
}

export function isNearBottom(list, threshold = 70) {
  return list.scrollHeight - list.scrollTop - list.clientHeight <= threshold;
}

export function maintainMessageViewport(list, entries, {atBottom, anchor, oldHeight}) {
  if (atBottom) list.scrollTop = list.scrollHeight;
  else if (anchor) restoreScrollAnchor(list, entries, anchor, oldHeight);
}

export function settleScrollBottom(list, scheduleFrame) {
  list.scrollTop = list.scrollHeight;
  scheduleFrame(() => {
    list.scrollTop = list.scrollHeight;
    scheduleFrame(() => { list.scrollTop = list.scrollHeight; });
  });
}

export function clipboardFile(data) {
  if (!data) return null;
  for (const item of Array.from(data.items || [])) {
    if (item?.kind !== 'file') continue;
    try {
      const file = item.getAsFile?.();
      if (file) return file;
    } catch (_) {}
  }
  return Array.from(data.files || []).find(Boolean) || null;
}

export function localImageClipboardUri(data) {
  if (!data || clipboardFile(data)) return '';
  let value = '';
  try { value = data.getData?.('text/uri-list') || data.getData?.('text/plain') || ''; } catch (_) {}
  value = String(value).trim();
  if (!value || /[\r\n]/.test(value)) return '';
  return /^file:\/\/\/[^?#]+\.(?:png|jpe?g|webp)(?:[?#].*)?$/i.test(value) ? value : '';
}

export function hasFileTransfer(data) {
  if (!data) return false;
  return Array.from(data.types || []).includes('Files') || Array.from(data.files || []).length > 0;
}

export function createFileDragTracker(onVisibilityChange) {
  let depth = 0, visible = false;
  const setVisible = value => {
    if (visible === value) return;
    visible = value; onVisibilityChange(value);
  };
  return {
    enter(allowed) {
      if (!allowed) return false;
      depth += 1; setVisible(true); return true;
    },
    leave() {
      if (depth <= 0) return false;
      depth -= 1;
      if (depth === 0) setVisible(false);
      return true;
    },
    reset() {
      const active = depth > 0 || visible;
      depth = 0; setVisible(false); return active;
    },
    get depth() { return depth; },
    get visible() { return visible; },
  };
}

export function messagesChanged(signatures, messages) {
  if (!(signatures instanceof Map) || signatures.size !== messages.length) return true;
  return messages.some(message => signatures.get(String(message.id)) !== JSON.stringify(message));
}

export function digestTitle(periodEnd) {
  const parsed = new Date(periodEnd);
  if (Number.isNaN(parsed.valueOf())) return 'Сводка';
  const date = parsed.toLocaleDateString('ru-RU', {
    day: 'numeric', month: 'short', timeZone: 'Europe/Moscow',
  });
  return `Сводка · ${date}`;
}

export function reactionEmojiPresentation(value) {
  const emoji = String(value || '◉');
  return emoji === '\u2764' ? `${emoji}\uFE0F` : emoji;
}

export function appendOnlyMessages(signatures, messages) {
  if (!(signatures instanceof Map) || !signatures.size) return null;
  const incoming = new Map(messages.map(message => [String(message.id), message]));
  for (const [id, signature] of signatures) {
    const message = incoming.get(id);
    if (!message || JSON.stringify(message) !== signature) return null;
  }
  const currentIds = [...signatures.keys()].map(Number);
  if (currentIds.some(id => !Number.isSafeInteger(id))) return null;
  const lastCurrentId = Math.max(...currentIds);
  const additions = messages.filter(message => !signatures.has(String(message.id)));
  if (!additions.length || additions.some(message => Number(message.id) <= lastCurrentId)) return null;
  return additions;
}

export function incrementalGrouping(messages, insertedIds) {
  const updates = [];
  let previous = null, previousDay = '', previousInserted = false;
  for (const message of messages) {
    const parsed = new Date(message.timestamp);
    const currentDay = Number.isNaN(parsed.valueOf()) ? 'unknown' : parsed.toISOString().slice(0, 10);
    if (currentDay !== previousDay) previous = null;
    const id = String(message.id), isInserted = insertedIds.has(id);
    if (isInserted || previousInserted) {
      updates.push([id, Boolean(previous) && !message.system && !previous.system
        && previous.sender === message.sender && previous.own === message.own]);
    }
    previous = message; previousDay = currentDay; previousInserted = isInserted;
  }
  return updates;
}

export function nextAuxiliaryPanel(current, requested) {
  if (!['library', 'notifications'].includes(requested)) return 'none';
  return current === requested ? 'none' : requested;
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
  return mode === 'inbox' || (['library', 'quick'].includes(mode) && source?.writable === true);
}

export function uploadWithinLimit(file, maxBytes) {
  return Boolean(file && Number.isSafeInteger(file.size) && file.size > 0
    && file.size <= maxBytes);
}

export function scopePath(mode, id) {
  const encoded = encodeURIComponent(String(id || ''));
  if (mode === 'inbox') return `/api/conversations/${encoded}`;
  if (mode === 'library') return `/api/library/${encoded}`;
  if (mode === 'quick') return `/api/quick-write/${encoded}`;
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

export function parseDeepLink(search, conversations, sources, quickSources = []) {
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
  const quick = params.get('write');
  if (quick && quickSources.some(row => row.id === quick)) {
    return {mode: 'quick', id: quick, messageId};
  }
  return null;
}

export function deepLinkFor(mode, id, messageId = null) {
  const params = new URLSearchParams();
  if (mode === 'inbox') params.set('conversation', id);
  if (mode === 'library') params.set('library', id);
  if (mode === 'quick') params.set('write', id);
  if (messageId && Number.isSafeInteger(Number(messageId)) && Number(messageId) > 0) {
    params.set('message', String(messageId));
  }
  const query = params.toString();
  return query ? `/?${query}` : '/';
}

export function isDigestHistoryRoute(search) {
  const params = new URLSearchParams(search || '');
  return params.get('view') === 'digests';
}

export function canonicalBadge(value) {
  const count = Number(value);
  return Number.isSafeInteger(count) && count > 0 ? count : 0;
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
    manual_write_enabled: Boolean(row.manual_write_enabled),
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
