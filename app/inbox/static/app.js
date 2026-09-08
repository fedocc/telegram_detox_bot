'use strict';
import {createPlaybackController} from './playback.mjs';
const playback = createPlaybackController(document);
window.addEventListener('pagehide', () => playback.stopAll());
// Removed/replaced bubbles must never keep playing outside the visible thread.
new MutationObserver(records => {
  for (const record of records) for (const removed of record.removedNodes) {
    if (removed.nodeType === 1 && !removed.isConnected) playback.pauseWithin(removed);
  }
}).observe(document.body, {childList:true, subtree:true});
const $ = id => document.getElementById(id);
let csrf = '', selected = null, conversations = [], serverOffset = 0, polling = false;
let messageNodes = new Map(), shownKey = null;
const drafts = new Map();
const now = () => Date.now() / 1000 + serverOffset;
const minutes = row => `${Math.max(1, Math.ceil((row.expires_at - now()) / 60))} мин`;
const initials = name => name.trim().split(/\s+/).slice(0, 2).map(x => x[0]).join('').toUpperCase();
function node(tag, className, text) { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = text; return n; }
function draft(key = selected) {
  if (!drafts.has(key)) {
    let text = ''; try { text = sessionStorage.getItem(`draft:${key}`) || ''; } catch (_) {}
    drafts.set(key, {text, file: null, url: null, requestId: null, sending: false, error: ''});
  }
  return drafts.get(key);
}
function saveDraft(d) { try { sessionStorage.setItem(`draft:${selected}`, d.text); } catch (_) {} }
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Inbox-CSRF':csrf}, body:JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'Ошибка соединения.'); error.status = response.status; throw error; }
  return result;
}
function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
function renderList() {
  $('count').textContent = conversations.length;
  const activeElement = document.activeElement?.dataset.key;
  const rows = conversations.map(row => {
    const button = node('button', `chat-row${selected === row.id ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.key = row.id;
    button.setAttribute('aria-current', selected === row.id ? 'true' : 'false');
    const top = node('div', 'row-top');
    top.append(node('span', 'avatar', initials(row.title)), node('span', 'row-title', row.title), node('span', 'age', `${Math.max(0, Math.floor((now()-row.activated_at)/60))}м`));
    button.append(top, node('p', 'preview', row.preview), node('span', 'row-time', `${minutes(row)} осталось`));
    button.onclick = () => choose(row.id); return button;
  });
  $('conversations').replaceChildren(...rows);
  if (activeElement) [...$('conversations').children].find(n => n.dataset.key === activeElement)?.focus();
}
function renderHeader() {
  const row = conversations.find(r => r.id === selected);
  $('empty').hidden = !!row; $('conversation').hidden = !row;
  if (!row) return;
  $('chat-title').textContent = row.title + (row.topic_title ? ` · ${row.topic_title}` : row.thread_id ? ` · Тема ${row.thread_id}` : '');
  $('chat-avatar').textContent = initials(row.title); $('remaining').textContent = minutes(row);
}
function renderComposer() {
  if (!selected) return;
  const d = draft(); $('text').value = d.text;
  $('attachment').hidden = !d.file;
  if (d.file) { $('attachment-preview').src = d.url; $('attachment-name').textContent = d.file.name; }
  else $('attachment-preview').removeAttribute('src');
  for (const id of ['text','attach','remove-image','close']) $(id).disabled = d.sending;
  $('send').disabled = d.sending || (!d.text.trim() && !d.file);
  $('send').setAttribute('aria-label', d.sending ? 'Отправка…' : 'Отправить сообщение');
  showError('send-error', d.error); resize();
}
function choose(key) {
  if (key === selected) return;
  selected = key; shownKey = null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren();
  showError('load-error', ''); renderList(); renderHeader(); renderComposer();
  loadMessages(key); $('text').focus();
}
function highlight(text, mention) {
  const p = node('p', 'message-text');
  if (!mention) { p.textContent = text; return p; }
  let last = 0;
  for (const match of text.matchAll(/(?<![A-Za-z0-9_])@fedocc(?![A-Za-z0-9_])/gi)) {
    p.append(document.createTextNode(text.slice(last, match.index)), node('mark', '', match[0]));
    last = match.index + match[0].length;
  }
  p.append(document.createTextNode(text.slice(last))); return p;
}
const sizeLabel = size => size >= 1048576 ? `${(size/1048576).toFixed(1)} МБ` : `${Math.ceil(size/1024)} КБ`;
const duration = seconds => `${Math.floor(seconds/60)}:${String(Math.floor(seconds%60)).padStart(2,'0')}`;
function voice(media) {
  const wrap = node('div', 'voice'), audio = node('audio'), play = node('button', '', '▷');
  audio.src = media.url; audio.preload = 'none'; play.type = 'button'; play.setAttribute('aria-label', 'Воспроизвести голосовое');
  const detail = node('div', 'voice-detail'), progress = node('input'), time = node('div', 'voice-time', duration(media.duration));
  progress.type = 'range'; progress.min = 0; progress.max = media.duration || 1; progress.value = 0; progress.step = .1;
  progress.setAttribute('aria-label', 'Позиция воспроизведения');
  play.onclick = async () => { if (!audio.paused) audio.pause(); else try { await audio.play(); } catch (_) { time.textContent = 'Аудио недоступно в этом браузере'; } };
  audio.onplay = () => { play.textContent = 'Ⅱ'; play.setAttribute('aria-label','Пауза'); };
  audio.onpause = () => { play.textContent = '▷'; play.setAttribute('aria-label','Воспроизвести голосовое'); };
  audio.ontimeupdate = () => { progress.max = audio.duration || media.duration || 1; progress.value = audio.currentTime; time.textContent = `${duration(audio.currentTime)} / ${duration(audio.duration || media.duration)}`; };
  progress.oninput = () => { if (Number.isFinite(audio.duration)) audio.currentTime = Number(progress.value); };
  detail.append(progress, time); wrap.append(play, detail, audio); return wrap;
}
function videoNote(media) {
  const wrap = node('div'), circle = node('div', 'video-note-shell');
  const video = node('video', 'media-video-note'), toggle = node('button', 'note-toggle', '▷');
  const time = node('div', 'video-note-time', duration(media.duration));
  video.src = media.url; video.preload = 'metadata'; video.playsInline = true;
  video.setAttribute('aria-label', 'Видеосообщение'); toggle.type = 'button';
  toggle.setAttribute('aria-label', 'Воспроизвести видеосообщение');
  toggle.onclick = async () => {
    if (!video.paused) video.pause();
    else try { await video.play(); } catch (_) { time.textContent = 'Видео недоступно в этом браузере'; }
  };
  video.onplay = () => { circle.classList.add('playing'); toggle.textContent = 'Ⅱ'; toggle.setAttribute('aria-label', 'Пауза видеосообщения'); };
  video.onpause = () => { circle.classList.remove('playing'); toggle.textContent = '▷'; toggle.setAttribute('aria-label', 'Воспроизвести видеосообщение'); };
  video.ontimeupdate = () => { time.textContent = `${duration(video.currentTime)} / ${duration(video.duration || media.duration)}`; };
  circle.append(video, toggle); wrap.append(circle, time); return wrap;
}
function attachment(media) {
  const wrap = node('div');
  if (!media.available) { wrap.append(node('div','media-note',`${media.name} · ${sizeLabel(media.size)} · превышает лимит загрузки 64 МБ`)); return wrap; }
  if (media.kind === 'photo') {
    const button = node('button', 'photo-button'), img = node('img');
    button.type = 'button'; button.setAttribute('aria-label', 'Открыть фотографию');
    img.src = media.url; img.alt = media.name; img.loading = 'lazy';
    button.onclick = () => { $('large-photo').src = media.url; $('photo-dialog').showModal(); };
    button.append(img); wrap.append(button);
  } else if (media.kind === 'video_note') {
    wrap.append(videoNote(media));
  } else if (media.kind === 'audio') {
    wrap.append(node('div', 'file-name', media.name));
    const audio = node('audio', 'media-audio'); audio.controls = true; audio.preload = 'none'; audio.src = media.url;
    audio.setAttribute('aria-label', `Аудиофайл: ${media.name}`); wrap.append(audio);
  } else if (media.kind === 'video') {
    const video = node('video', 'media-video'); video.controls = true; video.playsInline = true; video.preload = 'none'; video.src = media.url;
    video.setAttribute('aria-label', media.name); wrap.append(video);
  } else if (media.kind === 'voice') wrap.append(voice(media));
  if (media.kind === 'file') {
    const link = node('a', 'file'); link.href = media.url; link.download = media.name;
    const label = node('span'); label.append(node('span','file-name',media.name),node('span','file-size',sizeLabel(media.size)));
    link.append(node('span','file-icon','↓'),label); wrap.append(link);
  } else if (['voice', 'audio', 'video', 'video_note'].includes(media.kind)) {
    const link = node('a', 'media-note', `Скачать · ${sizeLabel(media.size)}`); link.href = media.url; link.download = media.name; wrap.append(link);
  }
  return wrap;
}
function messageNode(message) {
  const bubble = node('article', `bubble${message.own ? ' own' : ''}${message.mention ? ' mention' : ''}`);
  bubble.dataset.id = message.id;
  if (!message.own) {
    const sender = node('div','sender', message.sender);
    if (message.mention) sender.append(node('span','mention-badge','@ упом.'));
    bubble.append(sender);
  }
  if (message.reply) { const quote = node('div','quote'); quote.append(node('strong','',message.reply.sender),node('span','',message.reply.text)); bubble.append(quote); }
  if (message.media) bubble.append(attachment(message.media));
  if (message.text) bubble.append(highlight(message.text, message.mention));
  const timestamp = node('time','timestamp', new Date(message.timestamp).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}) + (message.own ? ' ✓' : ''));
  timestamp.dateTime = message.timestamp; timestamp.title = new Date(message.timestamp).toLocaleString('ru-RU'); bubble.append(timestamp); return bubble;
}
async function loadMessages(key) {
  try {
    const result = await api(`/api/conversations/${key}/messages`);
    if (selected !== key) return;
    const row = conversations.find(r => r.id === key); if (row) Object.assign(row, result.conversation);
    renderHeader(); showError('load-error','');
    const list = $('messages'), initial = shownKey !== key, atBottom = list.scrollHeight-list.scrollTop-list.clientHeight < 70;
    const keep = new Set(), order = []; let date = '', previousMessage = null;
    for (const m of result.messages) {
      const day = new Date(m.timestamp).toLocaleDateString('ru-RU',{day:'numeric',month:'long'});
      if (day !== date) {
        const dateKey = `day:${day}`; keep.add(dateKey); order.push(dateKey);
        if (!messageNodes.has(dateKey)) { const n = node('div','date',day); messageNodes.set(dateKey,{node:n}); list.append(n); }
        date = day;
      }
      const id = String(m.id), signature = JSON.stringify(m); keep.add(id); order.push(id);
      const existing = messageNodes.get(id);
      if (!existing) { const n = messageNode(m); messageNodes.set(id,{node:n,signature}); list.append(n); }
      else if (existing.signature !== signature) { const n = messageNode(m); playback.pauseWithin(existing.node); existing.node.replaceWith(n); messageNodes.set(id,{node:n,signature}); }
      messageNodes.get(id).node.classList.toggle('grouped', !!previousMessage && previousMessage.sender === m.sender && previousMessage.own === m.own);
      previousMessage = m;
    }
    for (const [id, value] of messageNodes) if (!keep.has(id)) { playback.pauseWithin(value.node); value.node.remove(); messageNodes.delete(id); }
    order.forEach((id, index) => { const n = messageNodes.get(id).node; if (list.children[index] !== n) list.insertBefore(n, list.children[index] || null); });
    shownKey = key;
    if (initial) { const trigger = messageNodes.get(String(result.conversation.trigger_id)); if (trigger) trigger.node.scrollIntoView({block:'center'}); else list.scrollTop = list.scrollHeight; }
    else if (atBottom) list.scrollTop = list.scrollHeight;
  } catch (error) { if (selected === key) showError('load-error', error.message); }
}
async function poll() {
  if (polling) return; polling = true;
  try {
    if (!csrf) { const session = await api('/api/session'); csrf = session.csrf; $('empty-description').textContent = `Чаты появляются после @fedocc или ответа на ваше сообщение и исчезают через ${session.active_minutes} мин.`; }
    const result = await api('/api/conversations'); serverOffset = result.now - Date.now()/1000;
    conversations = result.conversations.filter(r => r.expires_at > now());
    if (!conversations.some(r => r.id === selected)) { selected = null; shownKey = null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren(); }
    renderList(); renderHeader();
    if (!selected && conversations.length) choose(conversations[0].id);
    else if (selected) await loadMessages(selected);
    if (!result.connected) showError('load-error', 'Telegram переподключается…');
  } catch (_) { showError('load-error', 'Нет соединения · проверьте SSH-туннель'); }
  finally { polling = false; setTimeout(poll,2000); }
}
function resize() { $('text').style.height = 'auto'; $('text').style.height = `${Math.min(180,$('text').scrollHeight)}px`; }
$('text').oninput = () => { const d = draft(); d.text = $('text').value; d.requestId = null; d.error = ''; saveDraft(d); $('send').disabled = !d.text.trim() && !d.file; showError('send-error',''); resize(); };
$('text').onkeydown = event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } };
$('attach').onclick = () => $('image-input').click();
$('image-input').onchange = () => {
  const file = $('image-input').files[0]; $('image-input').value = ''; if (!file || !selected) return;
  const d = draft();
  if (!['image/jpeg','image/png','image/webp'].includes(file.type) || file.size > 10*1024*1024) { d.error = 'Выберите JPEG, PNG или WebP размером до 10 МБ.'; renderComposer(); return; }
  if (d.url) URL.revokeObjectURL(d.url);
  d.file = file; d.url = URL.createObjectURL(file); d.requestId = null; d.error = ''; renderComposer();
};
$('remove-image').onclick = () => { const d = draft(); if(d.url) URL.revokeObjectURL(d.url); d.file = null; d.url = null; d.requestId = null; renderComposer(); };
async function imageBase64(file) { const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ''; for(let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192)); return btoa(binary); }
$('composer').onsubmit = async event => {
  event.preventDefault(); if (!selected) return;
  const key = selected, d = draft(key); if (d.sending || (!d.text.trim() && !d.file)) return;
  d.sending = true; d.error = ''; d.requestId ||= crypto.randomUUID(); renderComposer();
  try {
    await api(`/api/conversations/${key}/send`, {request_id:d.requestId,text:d.text,...(d.file ? {image:await imageBase64(d.file)} : {})});
    d.text = ''; d.file = null; d.requestId = null; if(d.url) URL.revokeObjectURL(d.url); d.url = null;
    try { sessionStorage.removeItem(`draft:${key}`); } catch (_) {}
    if(selected === key) await loadMessages(key);
  } catch (error) { d.error = error.message || 'Нет ответа. Проверьте сообщения перед повтором; черновик сохранён.'; if (error.status === 403) csrf = ''; }
  finally { d.sending = false; if(selected === key) renderComposer(); }
};
$('close').onclick = async () => {
  const key = selected; if (!key || draft(key).sending) return;
  try { await api(`/api/conversations/${key}/close`, {}); conversations = conversations.filter(r=>r.id!==key); selected = null; shownKey = null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren(); renderList(); renderHeader(); if(conversations.length) choose(conversations[0].id); }
  catch(error) { showError('send-error', error.message); }
};
$('dismiss-photo').onclick = () => $('photo-dialog').close();
$('photo-dialog').addEventListener('close', () => $('large-photo').removeAttribute('src'));
setInterval(() => { conversations = conversations.filter(r=>r.expires_at>now()); if (selected && !conversations.some(r=>r.id===selected)) { selected=null; shownKey=null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren(); } renderList(); renderHeader(); },1000);
poll();
