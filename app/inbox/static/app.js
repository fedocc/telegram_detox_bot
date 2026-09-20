'use strict';

import {createPlaybackController} from './playback.mjs?v=11';
import {createNotificationToggle, notificationMode} from './notifications.mjs?v=11';
import {createPushController} from './push.mjs?v=11';
import {
  advanceOlderCursor, appendOnlyMessages, deepLinkFor, incrementalGrouping, isMacNotifierClient,
  isTerminalConversationStatus, insertNewNodesInOrder, isWritable, mergeMessagePages,
  messagesChanged, moveSelectedSource, newPageMessages, nextAuxiliaryPanel,
  normalizedSearchQuery, parseDeepLink, renderableMessages, restoreScrollAnchor,
  openedConversationIds, preferencePayload, retainFocusedMessage, scopePath, uploadWithinLimit,
  canonicalBadge, clipboardFile, createFileDragTracker, digestTitle, dismissDigest,
  hasFileTransfer, isDigestDismissed, isDigestHistoryRoute, isNearBottom,
  localImageClipboardUri, maintainMessageViewport, reactionEmojiPresentation,
  settleScrollBottom,
} from './ui.mjs?v=11';

const playback = createPlaybackController(document);
const $ = id => document.getElementById(id);
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

let csrf = '', selected = null, selectedMode = null, conversations = [], library = [];
let serverOffset = 0, polling = false, choosing = 0, shownKey = null;
let messageNodes = new Map(), nextBefore = null, olderLoading = false;
let olderCursorInitialized = false;
let selectedLoadPaused = false, uploadMax = 100 * 1024 * 1024, libraryPollAt = 0;
let libraryLoaded = false, routePending = true, routeWaitStarted = Date.now(), pinsGeneration = 0;
let searchGeneration = 0, searchTimer = null;
let searchState = {query: '', results: [], next: null, loading: false};
let managementDialogs = [], focusedMessageId = null;
let libraryManagementEnabled = false;
let pushController = null;
let auxiliaryPanel = 'none';
let fileDrag = null;
let appView = 'home', digestHistoryGeneration = 0;
const drafts = new Map();

window.addEventListener('pagehide', () => playback.stopAll());
const stickerObserver = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting) entry.target.play().catch(() => {});
    else entry.target.pause();
  }
}, {root: $('messages'), rootMargin: '120px'}) : null;
new MutationObserver(records => {
  for (const record of records) for (const removed of record.removedNodes) {
    if (removed.nodeType === 1 && !removed.isConnected) {
      playback.pauseWithin(removed);
      if (removed.matches?.('.sticker-video')) stickerObserver?.unobserve(removed);
      if (removed.matches?.('.custom-emoji-video')) stickerObserver?.unobserve(removed);
      for (const video of removed.querySelectorAll?.('.sticker-video,.custom-emoji-video') || []) {
        stickerObserver?.unobserve(video);
      }
    }
  }
}).observe(document.body, {childList: true, subtree: true});

const now = () => Date.now() / 1000 + serverOffset;
const visible = row => row.opened_at === null || row.expires_at > now();
const minutes = row => `${Math.max(1, Math.ceil((row.expires_at - now()) / 60))} мин`;
const initials = name => String(name || '').trim().split(/\s+/).slice(0, 2)
  .map(value => value[0] || '').join('').toUpperCase();
const currentSource = () => library.find(row => row.id === selected);
const currentScope = () => scopePath(selectedMode, selected);

async function api(path, data) {
  let response;
  try {
    response = await fetch(path, data === undefined ? {cache: 'no-store'} : {
      method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Inbox-CSRF': csrf},
      body: JSON.stringify(data),
    });
  } catch (_) {
    throw new Error('Нет соединения с сервисом.');
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || 'Ошибка соединения.');
    error.status = response.status;
    throw error;
  }
  return result;
}

async function multipartApi(path, form) {
  let response;
  try {
    response = await fetch(path, {
      method: 'POST', cache: 'no-store', headers: {'X-Inbox-CSRF': csrf}, body: form,
    });
  } catch (_) {
    throw new Error('Нет соединения с сервисом.');
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || 'Ошибка соединения.');
    error.status = response.status;
    throw error;
  }
  return result;
}

function showError(id, message) {
  $(id).textContent = message || '';
  $(id).hidden = !message;
}

function draft(key = selected) {
  const storageKey = `${selectedMode || 'none'}:${key || 'none'}`;
  if (!drafts.has(storageKey)) {
    let text = '';
    try { text = sessionStorage.getItem(`draft:${storageKey}`) || ''; } catch (_) {}
    drafts.set(storageKey, {
      storageKey, text, file: null, url: null, requestId: null,
      sending: false, error: '', reply: null,
    });
  }
  return drafts.get(storageKey);
}

function saveDraft(value) {
  try { sessionStorage.setItem(`draft:${value.storageKey}`, value.text); } catch (_) {}
}

function renderList() {
  $('count').textContent = conversations.length;
  const activeElement = document.activeElement?.dataset.key;
  const rows = conversations.map(row => {
    const active = selectedMode === 'inbox' && selected === row.id;
    const button = node('button', `chat-row${active ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.key = row.id;
    button.setAttribute('aria-current', active ? 'true' : 'false');
    const top = node('div', 'row-top');
    top.append(node('span', 'avatar', initials(row.title)), node('span', 'row-title', row.title),
      node('span', 'age', `${Math.max(0, Math.floor((now() - row.activated_at) / 60))}м`));
    if (row.unread_count) {
      const unread = node('span', 'unread', row.unread_count > 9 ? '9+' : String(row.unread_count));
      unread.setAttribute('aria-label', `${row.unread_count} непрочитанных`);
      top.append(unread);
    }
    button.append(top, node('p', 'preview', row.preview),
      node('span', 'row-time', row.opened_at === null ? 'Новое' : `${minutes(row)} осталось`));
    button.onclick = () => choose(row.id);
    const item = node('div', 'sidebar-item');
    const close = node('button', 'sidebar-close', '×');
    close.type = 'button'; close.setAttribute('aria-label', `Закрыть ${row.title}`);
    close.onclick = () => closeConversation(row.id);
    item.append(button, close);
    return item;
  });
  $('conversations').replaceChildren(...rows);
  if (activeElement) [...$('conversations').querySelectorAll('[data-key]')]
    .find(element => element.dataset.key === activeElement)?.focus();
}

function renderLibrary() {
  $('library-list').replaceChildren(...library.map(row => {
    const active = selectedMode === 'library' && selected === row.id;
    const button = node('button', `library-row${active ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.key = row.id;
    button.append(node('span', '', row.id === 'saved' ? `☆ ${row.title}` : row.title));
    const flags = [];
    if (row.notifications_muted) flags.push('без баннеров');
    if (row.is_bot && row.writable) flags.push('bot');
    if (flags.length) button.append(node('span', 'source-flags', flags.join(' · ')));
    button.onclick = () => chooseLibrary(row.id);
    return button;
  }));
}

function digestNodes(digest, emptyText) {
  const items = Array.isArray(digest.items) ? digest.items : [];
  const icons = {action: '⚡', study: '🎓', news: '📰', other: '•'};
  return items.length ? items.map(item => {
    const link = node('a', 'digest-item');
    link.href = item.links?.[0] || '#';
    link.append(node('span', 'digest-icon', icons[item.category] || '•'));
    const copy = node('span');
    copy.append(node('b', '', item.title), node('small', '', `${item.summary}  ↗`));
    link.append(copy);
    return link;
  }) : [node('p', 'digest-empty', emptyText)];
}

function renderMorningDigest(digest) {
  const card = $('morning-digest');
  let storage = null;
  try { storage = window.localStorage; } catch (_) {}
  if (!digest || isDigestDismissed(storage, digest)) {
    card.hidden = true; card.replaceChildren(); return;
  }
  const heading = node('strong', 'digest-heading', digestTitle(digest.period_end));
  const dismiss = node('button', 'digest-dismiss', '×');
  dismiss.type = 'button'; dismiss.setAttribute('aria-label', 'Скрыть сводку');
  dismiss.onclick = () => {
    dismissDigest(storage, digest);
    card.hidden = true; card.replaceChildren();
  };
  const content = digestNodes(digest, 'Ничего важного с прошлой сводки.');
  card.replaceChildren(heading, dismiss, ...content); card.hidden = false;
}

function renderDigestHistory(digests) {
  const cards = (digests || []).map(digest => {
    const card = node('article', 'digest-history-card');
    card.append(node('strong', 'digest-heading', digestTitle(digest.period_end)),
      ...digestNodes(digest, 'Ничего важного.'));
    return card;
  });
  $('digest-history-list').replaceChildren(...(cards.length ? cards : [
    node('p', 'digest-empty', 'За последнюю неделю сводок нет.'),
  ]));
}

function renderHeader() {
  const row = selectedMode === 'library' ? library.find(value => value.id === selected)
    : conversations.find(value => value.id === selected);
  const history = appView === 'digests';
  $('empty').hidden = Boolean(row) || history;
  $('digest-history').hidden = !history;
  $('conversation').hidden = !row;
  document.body.classList.toggle('conversation-open', Boolean(row));
  document.body.classList.toggle('digest-history-open', history);
  $('empty-title').textContent = conversations.length ? 'Выберите разговор' : 'Нет активных разговоров';
  if (!row) return;
  $('chat-title').textContent = row.title
    + (row.topic_title ? ` · ${row.topic_title}` : row.thread_id ? ` · Тема ${row.thread_id}` : '');
  $('chat-avatar').textContent = initials(row.title);
  $('remaining').hidden = selectedMode === 'library';
  $('remaining').textContent = selectedMode === 'library' ? '' : minutes(row);
  $('close').hidden = selectedMode === 'library';
  $('older').hidden = selectedMode !== 'library' || !nextBefore;
}

function resize() {
  $('text').style.height = 'auto';
  $('text').style.height = `${Math.min(180, $('text').scrollHeight)}px`;
}

function renderComposer() {
  if (!selected) return;
  const value = draft();
  $('text').value = value.text;
  const writable = isWritable(selectedMode, currentSource());
  $('conversation').querySelector('footer').hidden = !writable;
  $('composer').hidden = !writable;
  $('ghost-note').hidden = !(selectedMode === 'library' && currentSource()?.id === 'saved');
  if (!writable) { $('reply-target').hidden = true; return; }
  $('attachment').hidden = !value.file;
  if (value.file) {
    $('attachment-preview').hidden = !value.url;
    if (value.url) $('attachment-preview').src = value.url;
    $('attachment-name').textContent = value.file.name;
  } else {
    $('attachment-preview').hidden = false;
    $('attachment-preview').removeAttribute('src');
  }
  for (const id of ['text', 'attach', 'remove-image']) $(id).disabled = value.sending;
  $('close').disabled = value.sending;
  $('send').disabled = value.sending || (!value.text.trim() && !value.file);
  $('send').setAttribute('aria-label', value.sending ? 'Отправка…' : 'Отправить сообщение');
  showError('send-error', value.error); resize();
  $('reply-target').hidden = !value.reply;
  $('reply-target').querySelector('span').textContent = value.reply ? `Ответ: ${value.reply.text}` : '';
}

function updateLocation(messageId = null, {replace = false} = {}) {
  const url = selected ? deepLinkFor(selectedMode, selected, messageId) : '/';
  history[replace ? 'replaceState' : 'pushState'](null, '', url);
}

function resetSearch() {
  searchGeneration += 1;
  clearTimeout(searchTimer);
  searchState = {query: '', results: [], next: null, loading: false};
  $('search-input').value = '';
  $('search-results').replaceChildren();
  $('search-status').textContent = '';
  $('search-more').hidden = true;
  $('search-panel').hidden = true;
  $('search-toggle').setAttribute('aria-expanded', 'false');
}

function resetConversationSurface() {
  shownKey = null; nextBefore = null; olderLoading = false; olderCursorInitialized = false;
  selectedLoadPaused = false; focusedMessageId = null;
  messageNodes.clear();
  playback.pauseWithin($('messages')); $('messages').replaceChildren();
  $('older').hidden = true; $('older').disabled = false;
  $('older').textContent = 'Загрузить предыдущие сообщения';
  $('pinned-panel').hidden = true; $('pinned-list').replaceChildren();
  $('pinned-toggle').setAttribute('aria-expanded', 'false'); $('pinned-list').hidden = true;
  fileDrag?.reset();
  resetSearch();
}

function deselect({message = '', removeCurrent = false, replaceUrl = true} = {}) {
  const previous = selected, previousMode = selectedMode;
  choosing += 1; pinsGeneration += 1;
  if (removeCurrent && previousMode === 'inbox') {
    conversations = conversations.filter(row => row.id !== previous);
  }
  selected = null; selectedMode = null;
  appView = 'home'; digestHistoryGeneration += 1;
  resetConversationSurface();
  renderList(); renderLibrary(); renderHeader();
  showError('open-error', message); showError('load-error', '');
  if (replaceUrl) updateLocation(null, {replace: true});
}

function prepareSelection(mode, key) {
  const changed = selected !== key || selectedMode !== mode;
  appView = 'home'; digestHistoryGeneration += 1;
  selected = key; selectedMode = mode;
  if (changed) resetConversationSurface();
  selectedLoadPaused = false;
  showError('open-error', ''); showError('load-error', '');
  renderList(); renderLibrary(); renderHeader(); renderComposer();
  return changed;
}

async function choose(key, {updateHistory = true, messageId = null} = {}) {
  const request = ++choosing;
  try {
    const result = await api(`/api/conversations/${key}/open`, {});
    notificationToggle.conversationOpened(key);
    if (request !== choosing) return;
    conversations = conversations.map(row => row.id === key ? result.conversation : row);
    prepareSelection('inbox', key);
    syncBadge();
    if (messageId === null) focusedMessageId = null;
    if (updateHistory) updateLocation(null);
    await Promise.all([loadMessages(key), loadPins('inbox', key)]);
    if (request !== choosing) return;
    if (messageId) await focusMessage(messageId, {replaceUrl: true});
    else settleScrollBottom($('messages'), requestAnimationFrame);
    if (matchMedia('(min-width: 769px)').matches) $('text').focus();
  } catch (error) {
    if (request !== choosing) return;
    if (isTerminalConversationStatus(error.status)) {
      deselect({message: error.message, removeCurrent: true});
    } else showError(selected ? 'load-error' : 'open-error', error.message);
  }
}

async function chooseLibrary(key, {updateHistory = true, messageId = null} = {}) {
  if (!library.some(row => row.id === key)) return;
  const request = ++choosing;
  try {
    const opened = await api(`/api/library/${encodeURIComponent(key)}/open`, {});
    for (const conversationId of openedConversationIds(opened)) {
      notificationToggle.conversationOpened(conversationId);
    }
    if (request !== choosing) return;
    if (opened.source) library = library.map(row => row.id === key ? {...row, ...opened.source} : row);
    const changed = prepareSelection('library', key);
    syncBadge();
    auxiliaryPanel = 'none'; syncAuxiliaryPanels();
    if (messageId === null) focusedMessageId = null;
    if (updateHistory) updateLocation(null);
    await Promise.all([loadLibrary(key, null, !changed), loadPins('library', key)]);
    if (request !== choosing) return;
    if (messageId) await focusMessage(messageId, {replaceUrl: true});
    else settleScrollBottom($('messages'), requestAnimationFrame);
    if (currentSource()?.writable && matchMedia('(min-width: 769px)').matches) $('text').focus();
  } catch (error) {
    if (request !== choosing) return;
    if (isTerminalConversationStatus(error.status)) {
      await loadLibrarySources().catch(() => {});
      deselect({message: error.message});
    } else showError(selected ? 'load-error' : 'open-error', error.message);
  }
}

async function closeConversation(key) {
  if (!key || draft(key).sending) return;
  try {
    await api(`/api/conversations/${key}/close`, {});
    conversations = conversations.filter(row => row.id !== key);
    if (selectedMode === 'inbox' && selected === key) deselect();
    else renderList();
  } catch (error) {
    showError(selected ? 'send-error' : 'open-error', error.message);
  }
}

function safeHref(value) {
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
  } catch (_) { return null; }
}

function appendLinked(parent, text) {
  text = String(text || '');
  let last = 0;
  for (const match of text.matchAll(/https?:\/\/[^\s<>]+/gi)) {
    parent.append(document.createTextNode(text.slice(last, match.index)));
    const href = safeHref(match[0]);
    if (href) {
      const link = node('a', '', match[0]);
      link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      parent.append(link);
    } else parent.append(document.createTextNode(match[0]));
    last = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(last)));
}

function highlight(text, mention) {
  const result = node('span');
  if (!mention) { appendLinked(result, text); return result; }
  let last = 0;
  for (const match of text.matchAll(/(?<![A-Za-z0-9_])@fedocc(?![A-Za-z0-9_])/gi)) {
    appendLinked(result, text.slice(last, match.index)); result.append(node('mark', '', match[0]));
    last = match.index + match[0].length;
  }
  appendLinked(result, text.slice(last));
  return result;
}

function appendSegments(parent, segments, fallback, mention = false) {
  if (!Array.isArray(segments) || !segments.length) {
    parent.append(highlight(fallback, mention)); return;
  }
  for (const segment of segments) {
    const text = String(segment?.text || '');
    const href = segment?.url ? safeHref(segment.url) : null;
    const custom = segment?.custom_emoji;
    let content = null;
    if (custom?.available && custom.format === 'static') {
      const image = node('img', `custom-emoji${custom.text_color ? ' text-color' : ''}`);
      image.src = custom.url; image.alt = text; image.loading = 'lazy'; image.decoding = 'async';
      image.onerror = () => image.replaceWith(document.createTextNode(text));
      content = image;
    } else if (custom?.available && custom.format === 'video') {
      const video = node('video', 'custom-emoji custom-emoji-video');
      video.src = custom.url; video.muted = true; video.defaultMuted = true;
      video.loop = true; video.playsInline = true; video.preload = 'metadata';
      video.setAttribute('aria-label', text); video.setAttribute('role', 'img');
      video.onerror = () => video.replaceWith(document.createTextNode(text));
      stickerObserver?.observe(video);
      if (!stickerObserver) { video.autoplay = true; video.play().catch(() => {}); }
      content = video;
    }
    if (href) {
      const link = node('a', '', content ? undefined : text);
      link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      if (content) link.append(content);
      parent.append(link);
    } else if (content) parent.append(content);
    else if (mention) parent.append(highlight(text, true));
    else parent.append(document.createTextNode(text));
  }
}

function reactionIcon(reaction) {
  const fallback = String(reaction?.emoji || '◉');
  const custom = reaction?.custom_emoji;
  if (custom?.available && custom.format === 'static') {
    const image = node('img', 'custom-emoji');
    image.src = custom.url; image.alt = fallback; image.loading = 'lazy'; image.decoding = 'async';
    image.onerror = () => image.replaceWith(node('span', 'reaction-emoji', fallback));
    return image;
  }
  if (custom?.available && custom.format === 'video') {
    const video = node('video', 'custom-emoji custom-emoji-video');
    video.src = custom.url; video.muted = true; video.defaultMuted = true;
    video.loop = true; video.playsInline = true; video.preload = 'metadata';
    video.setAttribute('aria-label', fallback); video.setAttribute('role', 'img');
    video.onerror = () => video.replaceWith(node('span', 'reaction-emoji', fallback));
    stickerObserver?.observe(video);
    if (!stickerObserver) { video.autoplay = true; video.play().catch(() => {}); }
    return video;
  }
  return node('span', 'reaction-emoji', reactionEmojiPresentation(fallback));
}

function reactionsNode(reactions) {
  const wrap = node('div', 'message-reactions');
  for (const reaction of reactions || []) {
    if (!Number.isFinite(Number(reaction?.count)) || Number(reaction.count) <= 0) continue;
    const item = node('span', 'message-reaction');
    item.append(reactionIcon(reaction),
      node('span', 'reaction-count', String(Number(reaction.count))));
    wrap.append(item);
  }
  return wrap.childNodes.length ? wrap : null;
}

const sizeLabel = size => size >= 1048576
  ? `${(size / 1048576).toFixed(1)} МБ` : `${Math.ceil(size / 1024)} КБ`;
const duration = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

function voice(media) {
  const wrap = node('div', 'voice'), audio = node('audio'), play = node('button', '', '▷');
  audio.src = media.url; audio.preload = 'none'; play.type = 'button';
  play.setAttribute('aria-label', 'Воспроизвести голосовое');
  const detail = node('div', 'voice-detail'), progress = node('input');
  const time = node('div', 'voice-time', duration(media.duration));
  progress.type = 'range'; progress.min = 0; progress.max = media.duration || 1;
  progress.value = 0; progress.step = .1; progress.setAttribute('aria-label', 'Позиция воспроизведения');
  play.onclick = async () => {
    if (!audio.paused) audio.pause();
    else try { await audio.play(); } catch (_) { time.textContent = 'Аудио недоступно в этом браузере'; }
  };
  audio.onplay = () => { play.textContent = 'Ⅱ'; play.setAttribute('aria-label', 'Пауза'); };
  audio.onpause = () => { play.textContent = '▷'; play.setAttribute('aria-label', 'Воспроизвести голосовое'); };
  audio.ontimeupdate = () => {
    progress.max = audio.duration || media.duration || 1; progress.value = audio.currentTime;
    time.textContent = `${duration(audio.currentTime)} / ${duration(audio.duration || media.duration)}`;
  };
  progress.oninput = () => { if (Number.isFinite(audio.duration)) audio.currentTime = Number(progress.value); };
  detail.append(progress, time); wrap.append(play, detail, audio);
  return wrap;
}

function videoNote(media) {
  const wrap = node('div'), circle = node('div', 'video-note-shell');
  const video = node('video', 'media-video-note'), toggle = node('button', 'note-toggle', '▷');
  const time = node('div', 'video-note-time', duration(media.duration));
  video.src = media.url; video.preload = 'metadata'; video.playsInline = true;
  video.setAttribute('aria-label', 'Видеосообщение');
  toggle.type = 'button'; toggle.setAttribute('aria-label', 'Воспроизвести видеосообщение');
  toggle.onclick = async () => {
    if (!video.paused) video.pause();
    else try { await video.play(); } catch (_) { time.textContent = 'Видео недоступно в этом браузере'; }
  };
  video.onplay = () => { circle.classList.add('playing'); toggle.textContent = 'Ⅱ'; };
  video.onpause = () => { circle.classList.remove('playing'); toggle.textContent = '▷'; };
  video.ontimeupdate = () => {
    time.textContent = `${duration(video.currentTime)} / ${duration(video.duration || media.duration)}`;
  };
  circle.append(video, toggle); wrap.append(circle, time);
  return wrap;
}

function attachment(media) {
  const wrap = node('div');
  if (media.kind === 'sticker') {
    wrap.className = 'sticker-wrap';
    const fallback = failed => {
      const item = node('div', 'sticker-fallback', failed ? 'Не удалось загрузить стикер' : '[Стикер]');
      if (media.available) {
        const download = node('a', '', failed ? 'Повторить' : 'Скачать');
        download.href = media.url; download.download = media.name; item.append(download);
      }
      return item;
    };
    if (!media.available) { wrap.append(fallback(true)); return wrap; }
    if (media.sticker_format === 'static') {
      const image = node('img', 'sticker-media');
      image.src = media.url; image.alt = 'Стикер'; image.loading = 'lazy'; image.decoding = 'async';
      image.onerror = () => image.replaceWith(fallback(true)); wrap.append(image);
    } else if (media.sticker_format === 'video') {
      const video = node('video', 'sticker-media sticker-video');
      video.src = media.url; video.muted = true; video.defaultMuted = true;
      video.loop = true; video.playsInline = true; video.preload = 'metadata';
      video.setAttribute('aria-label', 'Стикер');
      video.onerror = () => video.replaceWith(fallback(true));
      wrap.append(video); stickerObserver?.observe(video);
      if (!stickerObserver) { video.autoplay = true; video.play().catch(() => {}); }
    } else wrap.append(fallback(false));
    return wrap;
  }
  if (!media.available) {
    wrap.append(node('div', 'media-note', `${media.name} · ${sizeLabel(media.size)} · превышает лимит загрузки 64 МБ`));
    return wrap;
  }
  if (media.kind === 'photo') {
    const button = node('button', 'photo-button'), image = node('img');
    button.type = 'button'; button.setAttribute('aria-label', 'Открыть фотографию');
    image.src = media.url; image.alt = media.name; image.loading = 'lazy';
    button.onclick = () => { $('large-photo').src = media.url; $('photo-dialog').showModal(); };
    button.append(image); wrap.append(button);
  } else if (media.kind === 'video_note') wrap.append(videoNote(media));
  else if (media.kind === 'audio') {
    wrap.append(node('div', 'file-name', media.name));
    const audio = node('audio', 'media-audio');
    audio.controls = true; audio.preload = 'none'; audio.src = media.url;
    audio.setAttribute('aria-label', `Аудиофайл: ${media.name}`); wrap.append(audio);
  } else if (media.kind === 'video') {
    const video = node('video', 'media-video');
    video.controls = true; video.playsInline = true; video.preload = 'none'; video.src = media.url;
    video.setAttribute('aria-label', media.name); wrap.append(video);
  } else if (media.kind === 'voice') wrap.append(voice(media));
  if (media.kind === 'file') {
    const link = node('a', 'file'); link.href = media.url; link.download = media.name;
    const label = node('span');
    label.append(node('span', 'file-name', media.name), node('span', 'file-size', sizeLabel(media.size)));
    link.append(node('span', 'file-icon', '↓'), label); wrap.append(link);
  } else if (['voice', 'audio', 'video', 'video_note'].includes(media.kind)) {
    const link = node('a', 'media-note', `Скачать · ${sizeLabel(media.size)}`);
    link.href = media.url; link.download = media.name; wrap.append(link);
  }
  return wrap;
}

function messageNode(message) {
  if (message.system) {
    const system = node('div', 'system-message', message.system);
    system.dataset.id = message.id;
    system.title = new Date(message.timestamp).toLocaleString('ru-RU');
    return system;
  }
  const stickerOnly = message.media?.kind === 'sticker' && !message.text;
  const bubble = node('article', `bubble${message.own ? ' own' : ''}${message.mention ? ' mention' : ''}${stickerOnly ? ' sticker-bubble' : ''}`);
  bubble.dataset.id = message.id;
  if (selectedMode === 'inbox' && !message.own) {
    const reply = node('button', 'reply-action', '↩');
    reply.type = 'button'; reply.title = 'Ответить';
    reply.onclick = () => {
      const value = draft();
      value.reply = {id: message.id, text: (message.text || message.media?.name || 'Вложение').slice(0, 120)};
      renderComposer(); $('text').focus();
    };
    bubble.append(reply);
  }
  if (!message.own) {
    const sender = node('div', 'sender', message.sender);
    if (message.mention) sender.append(node('span', 'mention-badge', '@ упом.'));
    bubble.append(sender);
  }
  if (message.reply) {
    const quote = node('div', 'quote'), text = node('span');
    appendSegments(text, message.reply.segments, message.reply.text);
    quote.append(node('strong', '', message.reply.sender), text); bubble.append(quote);
  }
  if (message.media) bubble.append(attachment(message.media));
  if (message.text) {
    const paragraph = node('p', 'message-text');
    appendSegments(paragraph, message.segments, message.text, message.mention); bubble.append(paragraph);
  }
  const reactions = reactionsNode(message.reactions);
  const timestamp = node('time', 'timestamp',
    new Date(message.timestamp).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'})
      + (message.own ? ' ✓' : ''));
  timestamp.dateTime = message.timestamp;
  timestamp.title = new Date(message.timestamp).toLocaleString('ru-RU');
  if (reactions) {
    const meta = node('div', 'message-meta');
    meta.append(reactions, timestamp); bubble.append(meta);
  } else bubble.append(timestamp);
  return bubble;
}

function messageDate(message) {
  const parsed = new Date(message.timestamp);
  if (Number.isNaN(parsed.valueOf())) return {key: 'unknown', label: ''};
  return {
    key: parsed.toISOString().slice(0, 10),
    label: parsed.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long', year: 'numeric'}),
  };
}

function dateSeparator(key, label) {
  const separator = node('div', 'date', label);
  separator.dataset.anchor = `day:${key}`;
  return separator;
}

function visibleAnchor(list) {
  const bounds = list.getBoundingClientRect();
  const xs = [bounds.left + bounds.width / 2, bounds.left + bounds.width / 4,
    bounds.right - bounds.width / 4];
  const ys = [bounds.top + 1, bounds.top + 10, bounds.top + 24];
  for (const y of ys) for (const x of xs) {
    const hit = document.elementFromPoint(x, Math.min(bounds.bottom - 1, y));
    const element = hit?.closest?.('[data-id],[data-anchor]');
    if (element && list.contains(element)) {
      return {id: element.dataset.id || element.dataset.anchor,
        top: element.getBoundingClientRect().top};
    }
  }
  return null;
}

function incrementalMessagePage(messages, key, {preserveAnchor = false} = {}) {
  if (shownKey !== key) return false;
  const existingIds = new Set([...messageNodes.entries()]
    .filter(([, value]) => value.message).map(([id]) => id));
  const additions = newPageMessages(existingIds, messages);
  if (!additions.length) return false;
  const list = $('messages');
  const atBottom = isNearBottom(list);
  const anchor = preserveAnchor ? visibleAnchor(list) : null, oldHeight = list.scrollHeight;
  const current = [...messageNodes.values()].filter(value => value.message)
    .map(value => value.message);
  const combined = mergeMessagePages(additions, current);
  const desired = [], inserted = new Set();
  let day = '';
  for (const message of combined) {
    const date = messageDate(message);
    if (date.key !== day) {
      const separatorKey = `day:${date.key}`;
      desired.push(separatorKey);
      if (!messageNodes.has(separatorKey)) {
        messageNodes.set(separatorKey, {node: dateSeparator(date.key, date.label)});
        inserted.add(separatorKey);
      }
      day = date.key;
    }
    const id = String(message.id);
    desired.push(id);
    if (!messageNodes.has(id)) {
      const signature = JSON.stringify(message), element = messageNode(message);
      messageNodes.set(id, {node: element, signature, message});
      inserted.add(id);
    }
  }
  insertNewNodesInOrder(list, desired, messageNodes, inserted);
  for (const [id, grouped] of incrementalGrouping(combined, inserted)) {
    messageNodes.get(id).node.classList.toggle('grouped', grouped);
  }
  if (preserveAnchor) restoreScrollAnchor(list, messageNodes, anchor, oldHeight);
  else if (atBottom) list.scrollTop = list.scrollHeight;
  return true;
}

function prependMessagePage(messages, key) {
  return incrementalMessagePage(messages, key, {preserveAnchor: true});
}

function focusLoadedMessage(messageId, {replaceUrl = false} = {}) {
  const item = messageNodes.get(String(messageId));
  if (!item?.node) return false;
  focusedMessageId = Number(messageId);
  item.node.scrollIntoView({block: 'center'});
  item.node.classList.remove('message-focus');
  requestAnimationFrame(() => item.node.classList.add('message-focus'));
  updateLocation(Number(messageId), {replace: replaceUrl});
  return true;
}

function renderMessages(messages, key, {prepend = false, merge = false, focusId = null} = {}) {
  const list = $('messages'), initial = shownKey !== key;
  messages = renderableMessages(messages);
  if (prepend && !initial) return prependMessagePage(messages, key);
  const current = [...messageNodes.values()].filter(value => value.message).map(value => value.message);
  const unique = merge ? mergeMessagePages(current, messages) : mergeMessagePages([], messages);
  const signatures = new Map([...messageNodes.entries()].filter(([, value]) => value.message)
    .map(([id, value]) => [id, value.signature]));
  if (!initial && !messagesChanged(signatures, unique)) return false;
  if (!initial && !prepend && appendOnlyMessages(signatures, unique)) {
    return incrementalMessagePage(unique, key);
  }
  const atBottom = isNearBottom(list);
  const oldHeight = list.scrollHeight;
  const anchor = !initial && !atBottom ? visibleAnchor(list) : null;
  const keep = new Set(), order = [];
  let date = '', previousMessage = null;
  for (const message of unique) {
    const parsedDate = messageDate(message), dayKey = parsedDate.key, day = parsedDate.label;
    if (dayKey !== date) {
      const separatorKey = `day:${dayKey}`;
      keep.add(separatorKey); order.push(separatorKey);
      if (!messageNodes.has(separatorKey)) {
        const separator = dateSeparator(dayKey, day);
        messageNodes.set(separatorKey, {node: separator}); list.append(separator);
      }
      date = dayKey; previousMessage = null;
    }
    const id = String(message.id), signature = JSON.stringify(message);
    keep.add(id); order.push(id);
    const existing = messageNodes.get(id);
    if (!existing) {
      const element = messageNode(message);
      messageNodes.set(id, {node: element, signature, message}); list.append(element);
    } else if (existing.signature !== signature) {
      const element = messageNode(message);
      playback.pauseWithin(existing.node); existing.node.replaceWith(element);
      messageNodes.set(id, {node: element, signature, message});
    }
    messageNodes.get(id).node.classList.toggle('grouped', Boolean(previousMessage)
      && !message.system && !previousMessage.system
      && previousMessage.sender === message.sender && previousMessage.own === message.own);
    previousMessage = message;
  }
  for (const [id, value] of messageNodes) if (!keep.has(id)) {
    playback.pauseWithin(value.node); value.node.remove(); messageNodes.delete(id);
  }
  order.forEach((id, index) => {
    const element = messageNodes.get(id).node;
    if (list.children[index] !== element) list.insertBefore(element, list.children[index] || null);
  });
  shownKey = key;
  if (prepend) {
    const anchored = anchor && messageNodes.get(anchor.id)?.node;
    if (anchored) list.scrollTop += anchored.getBoundingClientRect().top - anchor.top;
    else list.scrollTop += list.scrollHeight - oldHeight;
  } else if (focusId && messageNodes.has(String(focusId))) focusLoadedMessage(focusId, {replaceUrl: true});
  else if (initial) list.scrollTop = list.scrollHeight;
  else maintainMessageViewport(list, messageNodes, {atBottom, anchor, oldHeight});
  return true;
}

function pauseAutomaticHistory(error) {
  selectedLoadPaused = true;
  showError('load-error', `${error.message} Нажмите чат, чтобы повторить.`);
}

async function loadMessages(key) {
  try {
    const result = await api(`/api/conversations/${key}/messages`);
    if (selected !== key || selectedMode !== 'inbox') return;
    const row = conversations.find(value => value.id === key);
    if (row) Object.assign(row, result.conversation);
    selectedLoadPaused = false; renderHeader(); showError('load-error', '');
    const focused = messageNodes.get(String(focusedMessageId))?.message;
    const messages = retainFocusedMessage(result.messages || [], focusedMessageId, focused);
    renderMessages(messages, key);
  } catch (error) {
    if (selected !== key || selectedMode !== 'inbox') return;
    if (isTerminalConversationStatus(error.status)) {
      deselect({message: error.message, removeCurrent: true});
    } else pauseAutomaticHistory(error);
  }
}

async function loadLibrary(key, before = null, refresh = false) {
  if (olderLoading && before) return;
  if (before) {
    olderLoading = true; $('older').disabled = true; $('older').textContent = 'Загрузка…';
  }
  try {
    const suffix = before ? `?before=${encodeURIComponent(before)}` : '';
    const result = await api(`/api/library/${encodeURIComponent(key)}/messages${suffix}`);
    if (selected !== key || selectedMode !== 'library') return;
    if (before || !refresh || !olderCursorInitialized) {
      nextBefore = advanceOlderCursor(before, result.next_before);
      olderCursorInitialized = true;
      $('older').hidden = !nextBefore;
    }
    selectedLoadPaused = false;
    renderMessages(result.messages || [], `library:${key}`, {
      prepend: Boolean(before), merge: Boolean(before) || refresh,
    });
    showError('load-error', '');
  } catch (error) {
    if (selected !== key || selectedMode !== 'library') return;
    if (isTerminalConversationStatus(error.status)) {
      await loadLibrarySources().catch(() => {});
      deselect({message: error.message});
    } else pauseAutomaticHistory(error);
  } finally {
    if (before) {
      olderLoading = false; $('older').disabled = false;
      $('older').textContent = 'Загрузить предыдущие сообщения';
    }
  }
}

function renderPins(pins) {
  if (!pins.length) {
    $('pinned-panel').hidden = true; $('pinned-list').replaceChildren();
    $('pinned-toggle').setAttribute('aria-expanded', 'false');
    $('pinned-list').hidden = true; return;
  }
  $('pinned-count').textContent = pins.length > 1 ? `· ${pins.length}` : '';
  $('pinned-list').replaceChildren(...pins.map(pin => {
    const item = node('article', 'pin-item'), preview = node('div', 'pin-preview');
    appendSegments(preview, pin.segments, pin.system || pin.text || pin.media?.name || 'Сообщение');
    const open = node('button', '', 'Открыть');
    open.type = 'button'; open.onclick = () => focusMessage(pin.id);
    item.append(preview, open); return item;
  }));
  const expanded = $('pinned-toggle').getAttribute('aria-expanded') === 'true';
  $('pinned-list').hidden = !expanded; $('pinned-panel').hidden = false;
}

async function loadPins(mode, key) {
  const generation = ++pinsGeneration;
  try {
    const result = await api(`${scopePath(mode, key)}/pins`);
    if (generation !== pinsGeneration || selected !== key || selectedMode !== mode) return;
    renderPins(result.pins || result.messages || []);
  } catch (_) {
    if (generation === pinsGeneration && selected === key && selectedMode === mode) renderPins([]);
  }
}

async function focusMessage(messageId, {replaceUrl = false} = {}) {
  const numeric = Number(messageId);
  if (!Number.isSafeInteger(numeric) || numeric <= 0 || !selected) return;
  if (focusLoadedMessage(numeric, {replaceUrl})) return;
  try {
    const result = await api(`${currentScope()}/messages/${numeric}`);
    const messages = result.messages || (result.message ? [result.message] : []);
    if (!messages.some(value => Number(value.id) === numeric)) throw new Error('Сообщение недоступно.');
    const key = selectedMode === 'library' ? `library:${selected}` : selected;
    renderMessages(messages, key, {merge: true});
    focusLoadedMessage(numeric, {replaceUrl});
  } catch (error) { showError('load-error', error.message); }
}

function renderSearchResults() {
  $('search-results').replaceChildren(...searchState.results.map(message => {
    const item = node('article', 'search-result'), content = node('div');
    const meta = node('div', 'search-result-meta');
    meta.append(node('span', '', new Date(message.timestamp).toLocaleDateString('ru-RU', {day: 'numeric', month: 'long'})),
      node('span', '', message.sender || ''));
    const text = node('div', 'search-result-text');
    appendSegments(text, message.segments,
      message.system || message.text || message.media?.name || 'Вложение');
    content.append(meta, text);
    const open = node('button', '', 'Открыть');
    open.type = 'button'; open.onclick = () => focusMessage(message.id);
    item.append(content, open); return item;
  }));
  $('search-more').hidden = !searchState.next || searchState.loading;
  if (!searchState.loading) $('search-status').textContent = searchState.results.length
    ? `Найдено: ${searchState.results.length}` : 'Ничего не найдено';
}

async function executeSearch({append = false} = {}) {
  const query = normalizedSearchQuery($('search-input').value);
  if (!query) {
    searchState = {query: '', results: [], next: null, loading: false};
    $('search-results').replaceChildren(); $('search-more').hidden = true;
    $('search-status').textContent = 'Введите минимум 3 символа.'; return;
  }
  if (!selected || (append && searchState.loading)) return;
  const mode = selectedMode, key = selected, generation = ++searchGeneration;
  const before = append ? searchState.next : null;
  searchState.loading = true; $('search-status').textContent = 'Поиск…'; $('search-more').hidden = true;
  try {
    const params = new URLSearchParams({q: query});
    if (before !== null && before !== undefined && before !== '') params.set('before', before);
    const result = await api(`${scopePath(mode, key)}/search?${params}`);
    if (generation !== searchGeneration || selected !== key || selectedMode !== mode) return;
    const incoming = result.results || result.messages || [];
    const combined = append ? [...searchState.results, ...incoming] : incoming;
    searchState.results = [...new Map(combined.map(value => [value.id, value])).values()];
    searchState.query = query; searchState.next = result.next_before ?? result.next ?? null;
    searchState.loading = false; renderSearchResults();
  } catch (error) {
    if (generation !== searchGeneration) return;
    searchState.loading = false; $('search-status').textContent = error.message;
  }
}

function toggleSearch(open = $('search-panel').hidden) {
  $('search-panel').hidden = !open;
  $('search-toggle').setAttribute('aria-expanded', String(open));
  if (open) $('search-input').focus();
  else { searchGeneration += 1; clearTimeout(searchTimer); }
}

async function loadLibrarySources() {
  library = (await api('/api/library')).sources || [];
  const digest = await api('/api/digest/latest').catch(() => ({digest: null}));
  renderMorningDigest(digest.digest);
  libraryLoaded = true;
  renderLibrary();
  if (selectedMode === 'library' && !library.some(row => row.id === selected)) deselect();
}

async function showDigestHistory({updateHistory = true} = {}) {
  if (!matchMedia('(min-width: 769px)').matches) {
    deselect({replaceUrl: true}); return;
  }
  choosing += 1; pinsGeneration += 1;
  selected = null; selectedMode = null; appView = 'digests';
  const generation = ++digestHistoryGeneration;
  resetConversationSurface(); renderList(); renderLibrary(); renderHeader();
  showError('open-error', ''); showError('digest-history-error', '');
  $('digest-history-list').replaceChildren(node('p', 'digest-empty', 'Загрузка…'));
  if (updateHistory) history.pushState(null, '', '/?view=digests');
  try {
    const result = await api('/api/digest/history');
    if (generation !== digestHistoryGeneration || appView !== 'digests') return;
    renderDigestHistory(result.digests || []);
  } catch (error) {
    if (generation !== digestHistoryGeneration || appView !== 'digests') return;
    $('digest-history-list').replaceChildren();
    showError('digest-history-error', error.message);
  }
}

async function syncBadge() {
  if (!pushController) return;
  try {
    const result = await api('/api/badge');
    await pushController.setBadge(canonicalBadge(result.badge));
  } catch (_) {}
}

function managementRows() {
  const query = $('dialog-search').value.trim().toLocaleLowerCase('ru-RU');
  return query ? managementDialogs.filter(row => row.title.toLocaleLowerCase('ru-RU').includes(query))
    : managementDialogs;
}

function sortManagementDialogs() {
  const rank = new Map(library.map((row, index) => [row.id, index]));
  managementDialogs.sort((left, right) => {
    if (left.selected !== right.selected) return left.selected ? -1 : 1;
    return (rank.get(left.source_id) ?? Number.MAX_SAFE_INTEGER)
      - (rank.get(right.source_id) ?? Number.MAX_SAFE_INTEGER);
  });
}

async function savePreference(row, container, previous) {
  container.classList.add('busy');
  for (const control of container.querySelectorAll('input,button')) control.disabled = true;
  $('dialog-status').textContent = 'Сохраняю…';
  try {
    const result = await api('/api/library/preferences', preferencePayload(row));
    const source = result.source || result;
    if (source?.id) row.source_id = source.id;
    await loadLibrarySources();
    sortManagementDialogs(); renderManagement();
    $('dialog-status').textContent = 'Сохранено';
  } catch (error) {
    Object.assign(row, previous);
    $('dialog-status').textContent = error.message; renderManagement();
  }
}

function renderManagement() {
  $('dialog-list').replaceChildren(...managementRows().map(row => {
    const item = node('section', 'dialog-row'), main = node('div', 'dialog-main');
    const enabled = node('input');
    enabled.type = 'checkbox'; enabled.checked = Boolean(row.selected);
    enabled.setAttribute('aria-label', `Добавить ${row.title} в библиотеку`);
    main.append(enabled, node('span', 'dialog-title', row.title));
    if (row.is_bot) main.append(node('span', 'dialog-badge', 'BOT'));
    if (row.selected && row.source_id && row.source_id !== 'saved') {
      const order = node('span', 'dialog-order');
      const up = node('button', '', '↑'), down = node('button', '', '↓');
      up.type = down.type = 'button';
      up.setAttribute('aria-label', `Поднять ${row.title}`);
      down.setAttribute('aria-label', `Опустить ${row.title}`);
      up.onclick = () => reorderManagement(row.source_id, -1);
      down.onclick = () => reorderManagement(row.source_id, 1);
      order.append(up, down); main.append(order);
    }
    const options = node('div', 'dialog-options');
    const option = (labelText, property, disabled = false) => {
      const label = node('label'), input = node('input');
      input.type = 'checkbox'; input.checked = Boolean(row[property]); input.disabled = disabled;
      input.onchange = () => {
        const previous = {...row};
        row[property] = input.checked; savePreference(row, item, previous);
      };
      label.append(input, document.createTextNode(labelText)); return label;
    };
    options.append(option('Без системных уведомлений', 'notifications_muted', !row.selected),
      option('Не включать в будущий дайджест', 'digest_excluded', !row.selected));
    if (row.is_bot) options.append(option('Разрешить писать этому боту', 'allow_bot_write', !row.selected));
    options.hidden = !row.selected;
    enabled.onchange = () => {
      const previous = {...row};
      row.selected = enabled.checked;
      if (!row.selected) row.allow_bot_write = false;
      savePreference(row, item, previous);
    };
    item.append(main, options); return item;
  }));
}

async function reorderManagement(sourceId, direction) {
  const sourceIds = moveSelectedSource(managementDialogs, sourceId, direction);
  try {
    await api('/api/library/reorder', {source_ids: sourceIds});
    await loadLibrarySources();
    sortManagementDialogs(); renderManagement();
    $('dialog-status').textContent = 'Порядок сохранён';
  } catch (error) { $('dialog-status').textContent = error.message; }
}

async function loadManagementDialogs() {
  const result = await api('/api/library/dialogs');
  managementDialogs = result.dialogs || [];
  sortManagementDialogs();
  renderManagement(); return managementDialogs;
}

async function openLibraryManagement() {
  if (!libraryManagementEnabled) return;
  if (!$('library-dialog').open) $('library-dialog').showModal();
  $('dialog-status').textContent = 'Загружаю чаты…'; $('dialog-list').replaceChildren();
  try {
    await loadManagementDialogs();
    $('dialog-status').textContent = managementDialogs.length ? '' : 'Чаты не найдены.';
  } catch (error) { $('dialog-status').textContent = error.message; }
}

async function applyLocation() {
  if (isDigestHistoryRoute(location.search)) {
    routePending = false;
    await showDigestHistory({updateHistory: false}); return;
  }
  const route = parseDeepLink(location.search, conversations, library);
  if (!route) {
    const params = new URLSearchParams(location.search);
    const hasRoute = params.has('conversation') || params.has('library');
    if (!hasRoute) {
      routePending = false;
      if (selected || appView === 'digests') deselect({replaceUrl: false});
    } else if (Date.now() - routeWaitStarted > 30000 || params.has('library')) {
      routePending = false; deselect({message: 'Ссылка недоступна.', replaceUrl: true});
    }
    return;
  }
  routePending = false;
  if (route.mode === 'inbox') await choose(route.id, {updateHistory: false, messageId: route.messageId});
  else await chooseLibrary(route.id, {updateHistory: false, messageId: route.messageId});
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    if (!csrf) {
      const session = await api('/api/session');
      csrf = session.csrf; uploadMax = session.upload_max_mb * 1024 * 1024;
      libraryManagementEnabled = session.library_management_enabled === true;
      $('library-manage').hidden = !libraryManagementEnabled;
      $('empty-description').textContent = `Личные сообщения, @fedocc и ответы ожидают без таймера; после открытия доступны ${session.active_minutes} мин.`;
    }
    if (!libraryLoaded) await loadLibrarySources();
    const result = await api('/api/conversations');
    serverOffset = result.now - Date.now() / 1000;
    conversations = (result.conversations || []).filter(visible);
    pushController?.setBadge(canonicalBadge(result.badge));
    if (selectedMode === 'inbox'
        && !conversations.some(row => row.id === selected && row.opened_at !== null)) {
      deselect({replaceUrl: true});
    }
    renderList(); renderHeader();
    if (routePending) await applyLocation();
    if (selected && !selectedLoadPaused && selectedMode === 'inbox') await loadMessages(selected);
    if (selected && !selectedLoadPaused && selectedMode === 'library' && Date.now() >= libraryPollAt) {
      libraryPollAt = Date.now() + 10000; await loadLibrary(selected, null, true);
    }
    if (!result.connected) showError('load-error', 'Telegram переподключается…');
  } catch (error) {
    showError(selected ? 'load-error' : 'open-error', error.message || 'Нет соединения с сервисом.');
  } finally {
    polling = false; setTimeout(poll, 2000);
  }
}

$('text').oninput = () => {
  const value = draft();
  value.text = $('text').value; value.requestId = null; value.error = '';
  saveDraft(value); $('send').disabled = !value.text.trim() && !value.file;
  showError('send-error', ''); resize();
};
$('text').onkeydown = event => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
    event.preventDefault(); $('composer').requestSubmit();
  }
};
$('text').addEventListener('paste', event => {
  if (!selected || !isWritable(selectedMode, currentSource())) return;
  const file = clipboardFile(event.clipboardData);
  if (file) {
    event.preventDefault(); chooseFile(file); return;
  }
  if (localImageClipboardUri(event.clipboardData)) {
    event.preventDefault();
    const value = draft();
    value.error = 'Буфер содержит путь к локальному файлу, а не изображение. Скопируйте сам скриншот или перетащите файл.';
    renderComposer();
  }
});

function chooseFile(file) {
  if (!file || !selected || !isWritable(selectedMode, currentSource())) return;
  const value = draft();
  if (!uploadWithinLimit(file, uploadMax)) {
    value.error = `Файл пустой или больше ${Math.floor(uploadMax / 1048576)} МБ.`;
    renderComposer(); return;
  }
  if (value.url) URL.revokeObjectURL(value.url);
  value.file = file; value.url = file.type.startsWith('image/') ? URL.createObjectURL(file) : null;
  value.requestId = null; value.error = ''; renderComposer();
}

$('attach').onclick = () => $('image-input').click();
$('image-input').onchange = () => {
  const file = $('image-input').files[0]; $('image-input').value = ''; chooseFile(file);
};
$('remove-image').onclick = () => {
  const value = draft();
  if (value.url) URL.revokeObjectURL(value.url);
  value.file = null; value.url = null; value.requestId = null; renderComposer();
};
$('composer').onsubmit = async event => {
  event.preventDefault();
  if (!selected || !isWritable(selectedMode, currentSource())) return;
  const key = selected, mode = selectedMode, value = draft(key);
  if (value.sending || (!value.text.trim() && !value.file)) return;
  value.sending = true; value.error = ''; value.requestId ||= crypto.randomUUID(); renderComposer();
  try {
    const form = new FormData();
    form.set('request_id', value.requestId); form.set('text', value.text);
    if (value.file) form.set('file', value.file, value.file.name);
    if (value.reply) form.set('reply_to', String(value.reply.id));
    const path = mode === 'library' ? `/api/library/${encodeURIComponent(key)}/send`
      : `/api/conversations/${key}/upload`;
    await multipartApi(path, form);
    value.text = ''; value.file = null; value.reply = null; value.requestId = null;
    if (value.url) URL.revokeObjectURL(value.url);
    value.url = null;
    try { sessionStorage.removeItem(`draft:${value.storageKey}`); } catch (_) {}
    if (selected === key && selectedMode === mode) {
      if (mode === 'library') await loadLibrary(key, null, true);
      else await loadMessages(key);
    }
  } catch (error) {
    value.error = error.message || 'Нет ответа. Проверьте сообщения перед повтором; черновик сохранён.';
    if (error.status === 403) csrf = '';
  } finally {
    value.sending = false;
    if (selected === key && selectedMode === mode) renderComposer();
  }
};

$('close').onclick = () => closeConversation(selected);
$('home').onclick = () => deselect();
$('digest-history-button').onclick = () => showDigestHistory();
$('mobile-back').onclick = () => deselect();
$('reply-target').querySelector('button').onclick = () => { const value = draft(); value.reply = null; renderComposer(); };
let pauseMenuOpen = false;
function syncAuxiliaryPanels() {
  const libraryOpen = auxiliaryPanel === 'library';
  const notificationsOpen = auxiliaryPanel === 'notifications';
  document.body.classList.toggle('library-open', libraryOpen);
  document.body.classList.toggle('notifications-open', notificationsOpen);
  $('library-toggle').setAttribute('aria-expanded', String(libraryOpen));
  $('mobile-library').setAttribute('aria-expanded', String(libraryOpen));
  $('library-list').hidden = !libraryOpen;
  $('mobile-push').setAttribute('aria-expanded', String(notificationsOpen));
  $('push-panel').hidden = !(notificationsOpen
    && !document.body.classList.contains('notifier-client'));
  $('notifications-menu').hidden = !pauseMenuOpen;
  $('notifications-pause').setAttribute('aria-expanded', String(pauseMenuOpen));
}
function toggleAuxiliaryPanel(panel) {
  auxiliaryPanel = nextAuxiliaryPanel(auxiliaryPanel, panel);
  if (auxiliaryPanel !== 'notifications') pauseMenuOpen = false;
  syncAuxiliaryPanels();
}
$('library-toggle').onclick = () => toggleAuxiliaryPanel('library');
$('mobile-library').onclick = () => toggleAuxiliaryPanel('library');
$('library-manage').onclick = openLibraryManagement;
$('library-dialog-close').onclick = () => $('library-dialog').close();
$('library-dialog').addEventListener('click', event => {
  if (event.target === $('library-dialog')) $('library-dialog').close();
});
$('dialog-search').oninput = renderManagement;
$('older').onclick = () => {
  if (selectedMode === 'library' && nextBefore && !olderLoading) loadLibrary(selected, nextBefore);
};
$('pinned-toggle').onclick = () => {
  const open = $('pinned-toggle').getAttribute('aria-expanded') !== 'true';
  $('pinned-toggle').setAttribute('aria-expanded', String(open)); $('pinned-list').hidden = !open;
};
$('search-toggle').onclick = () => toggleSearch();
$('search-close').onclick = () => toggleSearch(false);
$('search-form').onsubmit = event => { event.preventDefault(); executeSearch(); };
$('search-input').oninput = () => {
  clearTimeout(searchTimer);
  if (!normalizedSearchQuery($('search-input').value)) {
    searchGeneration += 1;
    searchState = {query: '', results: [], next: null, loading: false};
    $('search-status').textContent = $('search-input').value.trim() ? 'Введите минимум 3 символа.' : '';
    $('search-results').replaceChildren(); $('search-more').hidden = true; return;
  }
  searchTimer = setTimeout(() => executeSearch(), 350);
};
$('search-more').onclick = () => executeSearch({append: true});

fileDrag = createFileDragTracker(visible => { $('drop-zone').hidden = !visible; });
$('conversation').addEventListener('dragenter', event => {
  const allowed = Boolean(selected && isWritable(selectedMode, currentSource())
    && hasFileTransfer(event.dataTransfer));
  if (fileDrag.enter(allowed)) event.preventDefault();
});
$('conversation').addEventListener('dragover', event => {
  if (selected && isWritable(selectedMode, currentSource())
      && hasFileTransfer(event.dataTransfer)) event.preventDefault();
});
$('conversation').addEventListener('dragleave', event => {
  if (fileDrag.leave()) event.preventDefault();
});
$('conversation').addEventListener('drop', event => {
  const allowed = Boolean(selected && isWritable(selectedMode, currentSource())
    && hasFileTransfer(event.dataTransfer));
  const active = fileDrag.reset();
  if (!allowed && !active) return;
  event.preventDefault();
  if (allowed) chooseFile(event.dataTransfer.files?.[0]);
});
window.addEventListener('dragend', () => fileDrag.reset());
$('dismiss-photo').onclick = () => $('photo-dialog').close();
$('photo-dialog').addEventListener('close', () => $('large-photo').removeAttribute('src'));

window.addEventListener('popstate', () => {
  routePending = true; routeWaitStarted = Date.now(); applyLocation();
});
window.addEventListener('pageshow', () => syncBadge());
window.addEventListener('focus', () => syncBadge());
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') syncBadge();
});

setInterval(() => {
  conversations = conversations.filter(visible);
  if (selectedMode === 'inbox' && selected
      && !conversations.some(row => row.id === selected && row.opened_at !== null)) deselect();
  renderList(); renderHeader();
}, 1000);

const notificationToggle = createNotificationToggle({
  async request(path, csrfToken, body = {}) {
    const response = await fetch(`http://127.0.0.1:8788${path}`, {
      method: csrfToken ? 'POST' : 'GET', cache: 'no-store', credentials: 'omit',
      signal: AbortSignal.timeout(3000),
      ...(csrfToken ? {
        headers: {'Content-Type': 'application/json', 'X-Notifier-CSRF': csrfToken},
        body: JSON.stringify(body),
      } : {}),
    });
    if (!response.ok) throw new Error('Bridge unavailable');
    return response.json();
  },
  render({enabled, effective_enabled, mute_until, available, busy, error, permission}) {
    const state = notificationMode({enabled, mute_until});
    for (const button of $('notifications-control').querySelectorAll('.notification-segments button')) {
      const selectedState = button.id === 'notifications-pause'
        ? 'pause' : button.dataset.notificationAction;
      button.disabled = !available || busy;
      button.setAttribute('aria-pressed', String(available && selectedState === state));
    }
    $('notifications-status').textContent = error || (!available ? 'Недоступны на этом Mac'
      : permission === 'denied' && enabled ? 'Разрешите в macOS' : '');
  },
});

const notifierClient = isMacNotifierClient({
  platform: navigator.userAgentData?.platform || navigator.platform,
  userAgent: navigator.userAgent,
  maxTouchPoints: navigator.maxTouchPoints,
});
if (notifierClient) {
  document.body.classList.add('notifier-client');
  $('notifications-control').hidden = false;
  $('notifications-pause').onclick = () => {
    const open = auxiliaryPanel !== 'notifications' || !pauseMenuOpen;
    auxiliaryPanel = open ? 'notifications' : 'none'; pauseMenuOpen = open;
    syncAuxiliaryPanels();
  };
  for (const button of document.querySelectorAll('[data-notification-action]')) {
    button.onclick = async () => {
      auxiliaryPanel = 'none'; pauseMenuOpen = false; syncAuxiliaryPanels();
      await notificationToggle.action(button.dataset.notificationAction);
    };
  }
  for (const button of document.querySelectorAll('[data-notification-duration]')) {
    button.onclick = async () => {
      await notificationToggle.action(button.dataset.notificationDuration);
      auxiliaryPanel = 'none'; pauseMenuOpen = false; syncAuxiliaryPanels();
    };
  }
  notificationToggle.refresh(); setInterval(notificationToggle.refresh, 15000);
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', async () => {
    try {
      const registration = await navigator.serviceWorker.register('/sw.js');
      pushController = createPushController({
        registration,
        request: (path, data) => api(path, data),
        notificationApi: window.Notification,
        badgeNavigator: navigator,
        render({available, configured, enabled, busy, denied, error}) {
          $('mobile-push').disabled = !available || busy;
          $('mobile-push').classList.toggle('active', enabled);
          $('push-enable').hidden = enabled || !available || !configured;
          $('push-disable').hidden = !enabled;
          $('push-status').textContent = error || (denied
            ? 'Уведомления запрещены. Разрешите их в настройках iOS для этого приложения.'
            : !available ? 'Установите приложение на экран «Домой» на поддерживаемой iOS.'
              : !configured ? 'Push ещё не настроен на сервере.'
                : enabled ? 'Уведомления включены на этом устройстве.'
                  : 'Включение доступно только по вашему нажатию.');
        },
      });
      await pushController.refresh();
      await syncBadge();
    } catch (_) { $('push-status').textContent = 'Push сейчас недоступен.'; }
  });
}

$('mobile-push').onclick = () => toggleAuxiliaryPanel('notifications');
$('push-enable').onclick = () => pushController?.enable();
$('push-disable').onclick = () => pushController?.disable();

syncAuxiliaryPanels();
poll();
