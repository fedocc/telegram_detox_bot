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

export function settleScrollBottom(list, scheduleFrame, shouldFollow = () => true) {
  if (!shouldFollow()) return;
  list.scrollTop = list.scrollHeight;
  scheduleFrame(() => {
    if (!shouldFollow()) return;
    list.scrollTop = list.scrollHeight;
    scheduleFrame(() => { if (shouldFollow()) list.scrollTop = list.scrollHeight; });
  });
}

export function createMessageViewport(list, {
  observeResize = callback => new ResizeObserver(callback),
  scheduleFrame = requestAnimationFrame,
} = {}) {
  let following = false, generation = 0, touchY = null;
  const observed = new Set();
  const bottom = () => {
    const target = Math.max(0, list.scrollHeight - list.clientHeight);
    if (following && Math.abs(list.scrollTop - target) > 1) list.scrollTop = target;
  };
  const observer = observeResize(() => bottom());
  const onScroll = () => { following = isNearBottom(list, 2); };
  const onWheel = event => { if (event.deltaY < 0) following = false; };
  const onTouchStart = event => { touchY = event.touches[0]?.clientY ?? null; };
  const onTouchMove = event => {
    const y = event.touches[0]?.clientY;
    if (touchY !== null && y > touchY) following = false;
    touchY = y ?? null;
  };
  const onKeyDown = event => {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)
        || event.key === ' ' && event.shiftKey) following = false;
  };
  list.addEventListener('scroll', onScroll, {passive: true});
  list.addEventListener('wheel', onWheel, {passive: true});
  list.addEventListener('touchstart', onTouchStart, {passive: true});
  list.addEventListener('touchmove', onTouchMove, {passive: true});
  list.addEventListener('keydown', onKeyDown);
  return {
    get following() { return following; },
    follow() {
      following = true;
      const current = ++generation;
      settleScrollBottom(list, scheduleFrame, () => following && current === generation);
    },
    release() { following = false; generation++; },
    watch() {
      const targets = new Set([list, ...list.children]);
      for (const target of observed) if (!targets.has(target)) {
        observer.unobserve(target); observed.delete(target);
      }
      for (const target of targets) if (!observed.has(target)) {
        observer.observe(target); observed.add(target);
      }
    },
    reset() {
      following = false; generation++; touchY = null;
      observer.disconnect(); observed.clear();
    },
    destroy() {
      this.reset();
      list.removeEventListener('scroll', onScroll);
      list.removeEventListener('wheel', onWheel);
      list.removeEventListener('touchstart', onTouchStart);
      list.removeEventListener('touchmove', onTouchMove);
      list.removeEventListener('keydown', onKeyDown);
    },
  };
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

export function bindReplyNavigation(quote, replyId, openMessage) {
  const id = Number(replyId);
  if (!Number.isSafeInteger(id) || id <= 0) return;
  quote.setAttribute('role', 'button');
  quote.setAttribute('aria-label', 'Открыть исходное сообщение');
  quote.tabIndex = 0;
  quote.title = 'Открыть исходное сообщение';
  quote.onclick = event => {
    if (!event.target.closest?.('a')) return openMessage(id);
  };
  quote.onkeydown = event => {
    if (event.target !== quote || !['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    return openMessage(id);
  };
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

export function manualOpenButtonVisible(quota, desktop = true) {
  const remaining = Number(quota?.remaining);
  return Boolean(desktop && Number.isInteger(remaining) && remaining > 0);
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

// Retain row nodes (and their loaded images/focus) across server snapshots.
export function reconcileKeyedRows(container, rows, views, create, update) {
  const keep = new Set(rows.map(row => String(row.id)));
  for (const [key, view] of views) if (!keep.has(key)) {
    view.element.remove(); views.delete(key);
  }
  rows.forEach((row, index) => {
    const key = String(row.id);
    if (!views.has(key)) views.set(key, create(row));
    const view = views.get(key);
    if (container.children[index] !== view.element) {
      container.insertBefore(view.element, container.children[index] || null);
    }
    update(view, row);
  });
}

// The keyboard changes the visible viewport on iOS without resizing 100dvh.
// Keep the app in that rectangle; only #messages should scroll inside a chat.
export function bindMobileViewport(win, root) {
  const mobile = win.matchMedia('(max-width: 768px)');
  const viewport = win.visualViewport;
  let frame = null;
  const values = new Map();
  const write = (name, value) => {
    if (values.get(name) === value) return;
    values.set(name, value);
    if (value === null) root.style.removeProperty(name);
    else root.style.setProperty(name, value);
  };
  const update = () => {
    if (!mobile.matches) {
      write('--app-viewport-height', null);
      write('--app-viewport-top', null);
      return;
    }
    // Preserve browser zoom: zooming must not shrink and reflow the app.
    if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
    const height = viewport?.height ?? win.innerHeight;
    if (!Number.isFinite(height) || height <= 0) return;
    const top = Math.max(0, viewport?.offsetTop || 0);
    write('--app-viewport-height', `${height}px`);
    write('--app-viewport-top', `${top}px`);
  };
  const schedule = () => {
    if (frame !== null) return;
    frame = win.requestAnimationFrame(() => { frame = null; update(); });
  };
  const events = [[win, 'resize'], [win, 'pageshow'], [mobile, 'change']];
  if (viewport) events.push([viewport, 'resize'], [viewport, 'scroll']);
  for (const [target, name] of events) target.addEventListener(name, schedule);
  update();
  return () => {
    for (const [target, name] of events) target.removeEventListener(name, schedule);
    if (frame !== null) win.cancelAnimationFrame(frame);
    write('--app-viewport-height', null);
    write('--app-viewport-top', null);
  };
}
