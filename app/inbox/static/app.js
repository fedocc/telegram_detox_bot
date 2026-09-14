'use strict';
import {createPlaybackController} from './playback.mjs';
import {createNotificationToggle, linkedConversation} from './notifications.mjs';
import {isWritable, mergeMessagePages, uploadWithinLimit} from './ui.mjs';
const playback = createPlaybackController(document);
window.addEventListener('pagehide', () => playback.stopAll());
// Removed/replaced bubbles must never keep playing outside the visible thread.
new MutationObserver(records => {
  for (const record of records) for (const removed of record.removedNodes) {
    if (removed.nodeType === 1 && !removed.isConnected) playback.pauseWithin(removed);
  }
}).observe(document.body, {childList:true, subtree:true});
const $ = id => document.getElementById(id);
let csrf = '', selected = null, selectedMode = null, conversations = [], library = [], serverOffset = 0, polling = false;
let messageNodes = new Map(), shownKey = null;
let nextBefore = null, uploadMax = 100 * 1024 * 1024;
let libraryPollAt = 0;
let notificationLinkPending = true;
const drafts = new Map();
const now = () => Date.now() / 1000 + serverOffset;
const visible = row => row.opened_at === null || row.expires_at > now();
let choosing = 0;
const minutes = row => `${Math.max(1, Math.ceil((row.expires_at - now()) / 60))} мин`;
const initials = name => name.trim().split(/\s+/).slice(0, 2).map(x => x[0]).join('').toUpperCase();
function node(tag, className, text) { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = text; return n; }
function draft(key = selected) {
  if (!drafts.has(key)) {
    let text = ''; try { text = sessionStorage.getItem(`draft:${key}`) || ''; } catch (_) {}
    drafts.set(key, {text, file: null, url: null, requestId: null, sending: false, error: '', reply:null});
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
async function multipartApi(path, form) {
  const response = await fetch(path, {method:'POST', headers:{'X-Inbox-CSRF':csrf}, body:form});
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'Ошибка соединения.'); error.status=response.status; throw error; }
  return result;
}
function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
function renderList() {
  $('count').textContent = conversations.length;
  const activeElement = document.activeElement?.dataset.key;
  const rows = conversations.map(row => {
    const button = node('button', `chat-row${selectedMode === 'inbox' && selected === row.id ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.key = row.id;
    button.setAttribute('aria-current', selected === row.id ? 'true' : 'false');
    const top = node('div', 'row-top');
    top.append(node('span', 'avatar', initials(row.title)), node('span', 'row-title', row.title), node('span', 'age', `${Math.max(0, Math.floor((now()-row.activated_at)/60))}м`));
    button.append(top, node('p', 'preview', row.preview), node('span', 'row-time', row.opened_at === null ? 'Новое' : `${minutes(row)} осталось`));
    button.onclick = () => choose(row.id);
    const item = node('div', 'sidebar-item'), close = node('button', 'sidebar-close', '×');
    close.type = 'button'; close.setAttribute('aria-label', `Закрыть ${row.title}`);
    close.onclick = () => closeConversation(row.id);
    item.append(button, close); return item;
  });
  $('conversations').replaceChildren(...rows);
  if (activeElement) [...$('conversations').querySelectorAll('[data-key]')].find(n => n.dataset.key === activeElement)?.focus();
}
function renderLibrary() {
  $('library-list').replaceChildren(...library.map(row => {
    const button=node('button',`library-row${selectedMode==='library'&&selected===row.id?' selected':''}`,row.title);
    button.type='button'; button.onclick=()=>chooseLibrary(row.id); return button;
  }));
}
function renderHeader() {
  const row = selectedMode === 'library' ? library.find(r=>r.id===selected) : conversations.find(r => r.id === selected);
  $('empty').hidden = !!row; $('conversation').hidden = !row;
  $('empty-title').textContent = conversations.length ? 'Выберите разговор' : 'Нет активных разговоров';
  if (!row) return;
  $('chat-title').textContent = row.title + (row.topic_title ? ` · ${row.topic_title}` : row.thread_id ? ` · Тема ${row.thread_id}` : '');
  $('chat-avatar').textContent = initials(row.title);
  $('remaining').hidden = selectedMode === 'library';
  $('remaining').textContent = selectedMode === 'library' ? '' : minutes(row);
  $('close').hidden = selectedMode === 'library';
}
function renderComposer() {
  if (!selected) return;
  const d = draft(); $('text').value = d.text;
  const source = library.find(row=>row.id===selected);
  const writable = isWritable(selectedMode, source);
  $('conversation').querySelector('footer').hidden=!writable;
  $('composer').hidden = !writable; $('ghost-note').hidden = !(selectedMode==='library'&&source?.id==='saved');
  if (!writable) { $('reply-target').hidden=true; return; }
  $('attachment').hidden = !d.file;
  if (d.file) { $('attachment-preview').hidden=!d.url; if(d.url)$('attachment-preview').src = d.url; $('attachment-name').textContent = d.file.name; }
  else { $('attachment-preview').hidden=false; $('attachment-preview').removeAttribute('src'); }
  for (const id of ['text','attach','remove-image']) $(id).disabled = d.sending;
  $('close').disabled = d.sending;
  $('send').disabled = d.sending || (!d.text.trim() && !d.file);
  $('send').setAttribute('aria-label', d.sending ? 'Отправка…' : 'Отправить сообщение');
  showError('send-error', d.error); resize();
  $('reply-target').hidden=!d.reply; $('reply-target').querySelector('span').textContent=d.reply ? `Ответ: ${d.reply.text}` : '';
}
async function choose(key) {
  if (key === selected) return;
  const request = ++choosing;
  try {
    const result = await api(`/api/conversations/${key}/open`, {});
    if (request !== choosing) return;
    conversations = conversations.map(row => row.id === key ? result.conversation : row);
    selected = key; selectedMode='inbox'; shownKey = null; messageNodes.clear();
    playback.pauseWithin($('messages')); $('messages').replaceChildren();
    showError('open-error', ''); showError('load-error', '');
    renderList(); renderHeader(); renderComposer();
    loadMessages(key); $('text').focus();
  } catch (error) {
    if (request === choosing) showError(selected ? 'load-error' : 'open-error', error.message);
  }
}
async function chooseLibrary(key) {
  if (key===selected && selectedMode==='library') { await loadLibrary(key); return; }
  ++choosing; selected=key; selectedMode='library'; shownKey=null; nextBefore=null; messageNodes.clear();
  playback.pauseWithin($('messages')); $('messages').replaceChildren();
  showError('open-error',''); showError('load-error',''); renderList(); renderLibrary(); renderHeader(); renderComposer();
  await loadLibrary(key); if (library.find(row=>row.id===key)?.writable) $('text').focus();
}
async function closeConversation(key) {
  if (!key || draft(key).sending) return;
  ++choosing;
  try {
    await api(`/api/conversations/${key}/close`, {});
    conversations = conversations.filter(row => row.id !== key);
    if (selected === key) {
      selected = null; selectedMode=null; shownKey = null; messageNodes.clear();
      playback.pauseWithin($('messages')); $('messages').replaceChildren();
    }
    renderList(); renderHeader();
  } catch (error) { showError(selected ? 'send-error' : 'open-error', error.message); }
}

function appendLinked(parent, text) {
  let last=0;
  for (const match of text.matchAll(/https?:\/\/[^\s<>]+/gi)) {
    parent.append(document.createTextNode(text.slice(last,match.index)));
    const link=node('a','',match[0]); link.href=match[0]; link.target='_blank'; link.rel='noopener noreferrer'; parent.append(link);
    last=match.index+match[0].length;
  }
  parent.append(document.createTextNode(text.slice(last)));
}
function highlight(text, mention) {
  const p = node('p', 'message-text');
  if (!mention) { appendLinked(p,text); return p; }
  let last = 0;
  for (const match of text.matchAll(/(?<![A-Za-z0-9_])@fedocc(?![A-Za-z0-9_])/gi)) {
    appendLinked(p,text.slice(last, match.index)); p.append(node('mark', '', match[0]));
    last = match.index + match[0].length;
  }
  appendLinked(p,text.slice(last)); return p;
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
  if (selectedMode==='inbox' && !message.own) {
    const reply=node('button','reply-action','↩'); reply.type='button'; reply.title='Ответить';
    reply.onclick=()=>{ const d=draft(); d.reply={id:message.id,text:(message.text||message.media?.name||'Вложение').slice(0,120)}; renderComposer(); $('text').focus(); };
    bubble.append(reply);
  }
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
function renderMessages(messages, key, {triggerId=null, prepend=false, merge=false}={}) {
    const list = $('messages'), initial = shownKey !== key, atBottom = list.scrollHeight-list.scrollTop-list.clientHeight < 70;
    const oldHeight=list.scrollHeight;
    const current=[...messageNodes.values()].filter(x=>x.message).map(x=>x.message);
    const all = merge ? [...messages,...current] : messages;
    const unique=mergeMessagePages([],all);
    const keep = new Set(), order = []; let date = '', previousMessage = null;
    for (const m of unique) {
      const day = new Date(m.timestamp).toLocaleDateString('ru-RU',{day:'numeric',month:'long'});
      if (day !== date) {
        const dateKey = `day:${day}`; keep.add(dateKey); order.push(dateKey);
        if (!messageNodes.has(dateKey)) { const n = node('div','date',day); messageNodes.set(dateKey,{node:n}); list.append(n); }
        date = day;
      }
      const id = String(m.id), signature = JSON.stringify(m); keep.add(id); order.push(id);
      const existing = messageNodes.get(id);
      if (!existing) { const n = messageNode(m); messageNodes.set(id,{node:n,signature,message:m}); list.append(n); }
      else if (existing.signature !== signature) { const n = messageNode(m); playback.pauseWithin(existing.node); existing.node.replaceWith(n); messageNodes.set(id,{node:n,signature,message:m}); }
      messageNodes.get(id).node.classList.toggle('grouped', !!previousMessage && previousMessage.sender === m.sender && previousMessage.own === m.own);
      previousMessage = m;
    }
    for (const [id, value] of messageNodes) if (!keep.has(id)) { playback.pauseWithin(value.node); value.node.remove(); messageNodes.delete(id); }
    order.forEach((id, index) => { const n = messageNodes.get(id).node; if (list.children[index] !== n) list.insertBefore(n, list.children[index] || null); });
    shownKey = key;
    if (prepend) list.scrollTop += list.scrollHeight-oldHeight;
    else if (initial) { const trigger = messageNodes.get(String(triggerId)); if (trigger) trigger.node.scrollIntoView({block:'center'}); else list.scrollTop = list.scrollHeight; }
    else if (atBottom) list.scrollTop = list.scrollHeight;
}
async function loadMessages(key) {
  try {
    const result = await api(`/api/conversations/${key}/messages`);
    if (selected !== key || selectedMode!=='inbox') return;
    const row = conversations.find(r => r.id === key); if (row) Object.assign(row, result.conversation);
    renderHeader(); showError('load-error','');
    renderMessages(result.messages,key,{triggerId:result.conversation.trigger_id});
  } catch (error) { if (selected === key) showError('load-error', error.message); }
}
async function loadLibrary(key, before=null, refresh=false) {
  try {
    const suffix=before?`?before=${before}`:'';
    const result=await api(`/api/library/${key}/messages${suffix}`);
    if(selected!==key||selectedMode!=='library') return;
    if(!refresh) nextBefore=result.next_before; $('older').hidden=!nextBefore;
    renderMessages(result.messages,`library:${key}`,{prepend:!!before,merge:!!before||refresh}); showError('load-error','');
  } catch(error) { if(selected===key&&selectedMode==='library') showError('load-error',error.message); }
}
async function poll() {
  if (polling) return; polling = true;
  try {
    if (!csrf) {
      const session = await api('/api/session'); csrf = session.csrf; uploadMax=session.upload_max_mb*1024*1024;
      $('empty-description').textContent = `Личные сообщения, @fedocc и ответы ожидают без таймера; после открытия доступны ${session.active_minutes} мин.`;
      library=(await api('/api/library')).sources; renderLibrary();
    }
    const result = await api('/api/conversations'); serverOffset = result.now - Date.now()/1000;
    conversations = result.conversations.filter(visible);
    if (selectedMode==='inbox' && !conversations.some(r => r.id === selected && r.opened_at !== null)) { selected = null; selectedMode=null; shownKey = null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren(); }
    renderList(); renderHeader();
    if (notificationLinkPending) {
      notificationLinkPending = false;
      const key = linkedConversation(location.search, conversations);
      if (new URLSearchParams(location.search).has('conversation')) history.replaceState(null, '', '/');
      if (key) await choose(key);
    }
    if (selected && selectedMode==='inbox') await loadMessages(selected);
    if (selected && selectedMode==='library' && Date.now() >= libraryPollAt) {
      libraryPollAt=Date.now()+10000; await loadLibrary(selected,null,true);
    }
    if (!result.connected) showError('load-error', 'Telegram переподключается…');
  } catch (_) { showError('load-error', 'Нет соединения · проверьте SSH-туннель'); }
  finally { polling = false; setTimeout(poll,2000); }
}
function resize() { $('text').style.height = 'auto'; $('text').style.height = `${Math.min(180,$('text').scrollHeight)}px`; }
$('text').oninput = () => { const d = draft(); d.text = $('text').value; d.requestId = null; d.error = ''; saveDraft(d); $('send').disabled = !d.text.trim() && !d.file; showError('send-error',''); resize(); };
$('text').onkeydown = event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } };
function chooseFile(file) {
  if (!file || !selected) return;
  const d = draft();
  if (!uploadWithinLimit(file,uploadMax)) { d.error = `Файл пустой или больше ${Math.floor(uploadMax/1048576)} МБ.`; renderComposer(); return; }
  if (d.url) URL.revokeObjectURL(d.url);
  d.file = file; d.url = file.type.startsWith('image/') ? URL.createObjectURL(file) : null; d.requestId = null; d.error = ''; renderComposer();
}
$('attach').onclick = () => $('image-input').click();
$('image-input').onchange = () => { const file=$('image-input').files[0]; $('image-input').value=''; chooseFile(file); };
$('remove-image').onclick = () => { const d = draft(); if(d.url) URL.revokeObjectURL(d.url); d.file = null; d.url = null; d.requestId = null; renderComposer(); };
$('composer').onsubmit = async event => {
  event.preventDefault(); if (!selected) return;
  const key = selected, d = draft(key); if (d.sending || (!d.text.trim() && !d.file)) return;
  d.sending = true; d.error = ''; d.requestId ||= crypto.randomUUID(); renderComposer();
  try {
    const form=new FormData(); form.set('request_id',d.requestId); form.set('text',d.text);
    if(d.file) form.set('file',d.file,d.file.name); if(d.reply) form.set('reply_to',String(d.reply.id));
    const path=selectedMode==='library' ? '/api/library/saved/send' : `/api/conversations/${key}/upload`;
    await multipartApi(path,form);
    d.text = ''; d.file = null; d.reply=null; d.requestId = null; if(d.url) URL.revokeObjectURL(d.url); d.url = null;
    try { sessionStorage.removeItem(`draft:${key}`); } catch (_) {}
    if(selected === key) { if(selectedMode==='library') await loadLibrary(key); else await loadMessages(key); }
  } catch (error) { d.error = error.message || 'Нет ответа. Проверьте сообщения перед повтором; черновик сохранён.'; if (error.status === 403) csrf = ''; }
  finally { d.sending = false; if(selected === key) renderComposer(); }
};
$('close').onclick = () => closeConversation(selected);
$('reply-target').querySelector('button').onclick=()=>{ const d=draft(); d.reply=null; renderComposer(); };
$('library-toggle').onclick=()=>{ const open=$('library-toggle').getAttribute('aria-expanded')!=='true'; $('library-toggle').setAttribute('aria-expanded',String(open)); $('library-list').hidden=!open; };
$('older').onclick=()=>{ if(selectedMode==='library'&&nextBefore) loadLibrary(selected,nextBefore); };
for(const type of ['dragenter','dragover']) $('conversation').addEventListener(type,event=>{ if(selected&&(selectedMode==='inbox'||library.find(x=>x.id===selected)?.writable)){event.preventDefault();$('drop-zone').hidden=false;} });
for(const type of ['dragleave','drop']) $('conversation').addEventListener(type,event=>{event.preventDefault();$('drop-zone').hidden=true;if(type==='drop')chooseFile(event.dataTransfer.files[0]);});
$('dismiss-photo').onclick = () => $('photo-dialog').close();
$('photo-dialog').addEventListener('close', () => $('large-photo').removeAttribute('src'));
setInterval(() => { conversations = conversations.filter(visible); if (selectedMode==='inbox' && selected && !conversations.some(r=>r.id===selected && r.opened_at !== null)) { selected=null; selectedMode=null; shownKey=null; messageNodes.clear(); playback.pauseWithin($('messages')); $('messages').replaceChildren(); } renderList(); renderHeader(); },1000);
poll();

const notificationToggle = createNotificationToggle({
  async request(path, csrfToken) {
    const response = await fetch(`http://127.0.0.1:8788${path}`, {
      method: csrfToken ? 'POST' : 'GET', cache: 'no-store', credentials: 'omit',
      signal: AbortSignal.timeout(3000),
      ...(csrfToken ? {headers: {'Content-Type': 'application/json', 'X-Notifier-CSRF': csrfToken}, body: '{}'} : {}),
    });
    if (!response.ok) throw new Error('Bridge unavailable');
    return response.json();
  },
  render({enabled, available, busy, error, permission}) {
    const button = $('notifications-toggle');
    button.disabled = !available || busy;
    button.setAttribute('aria-checked', String(available && enabled));
    button.textContent = available ? (enabled ? 'ON' : 'OFF') : '—';
    $('notifications-status').textContent = error || (!available ? 'Недоступны' : permission === 'denied' && enabled ? 'Разрешите в macOS' : '');
  },
});
$('notifications-toggle').onclick = notificationToggle.toggle;
notificationToggle.refresh();
setInterval(notificationToggle.refresh, 15000);
