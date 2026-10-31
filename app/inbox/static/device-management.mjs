import {reconcileKeyedRows} from './ui.mjs?v=25';
const $ = id => document.getElementById(id), views = new Map(), auditViews = new Map();
let csrf = '', invitation = null, imageURL = null, loading = false;
const node = (tag, text = '') => { const e = document.createElement(tag); e.textContent = text; return e; };
const set = (e, text) => { if (e.textContent !== text) e.textContent = text; };
const date = value => value ? new Date(value * 1000).toLocaleString('ru-RU') : 'ещё не входило';
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {
    method:'POST', cache:'no-store', headers:{'Content-Type':'application/json','X-Inbox-CSRF':csrf},
    body:JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие.');
  return data;
}
async function action(work) {
  $('device-status').textContent = '';
  try { await work(); await refresh(); }
  catch (error) { $('device-status').textContent = error.message || 'Нет соединения.'; }
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    if (!csrf) csrf = (await api('/api/session')).csrf;
    const data = await api('/api/devices');
    reconcileKeyedRows($('devices'), data.devices, views, row => {
      const element = node('article'); element.className = 'device-row';
      const title=node('strong'), detail=node('p'), code=node('p'), button=node('button');
      button.type='button'; element.append(title,detail,code,button);
      return {element,title,detail,code,button};
    }, (view,row) => {
      const pending=!row.approved_at&&!row.revoked_at, revoked=Boolean(row.revoked_at);
      const expired=pending && row.created_at*1000 < Date.now()-900000 || row.approved_at && row.trusted_until*1000 <= Date.now();
      set(view.title,row.label);
      set(view.detail,revoked?'Доступ отозван':expired?'Срок подключения истёк':pending?'Ожидает подтверждения':
        `Последний вход: ${date(row.last_used_at)}${row.last_ip?' · '+row.last_ip:''}`);
      set(view.code,pending?`Код на iPhone: ${row.code.match(/.{1,3}/g).join(' ')}`:'');
      view.code.className=pending?'device-confirm-code':'';
      view.button.hidden=revoked; view.button.disabled=Boolean(expired && pending); view.button.className=pending?'auth-primary':'auth-secondary';
      set(view.button,pending?'Код совпадает — подтвердить':'Отозвать доступ');
      view.button.onclick=()=>action(async()=>{
        view.button.disabled=true;
        try { await api(`/api/devices/${row.id}/${pending?'approve':'revoke'}`,pending?{code:row.code}:{}); }
        finally { view.button.disabled=false; }
      });
    });
    $('no-devices').hidden=Boolean(data.devices.length);
    const names={invitation_created:'Создано приглашение',invitation_cancelled:'Приглашение отменено',
      device_pending:'Запрос на подключение',device_approved:'Устройство подтверждено',
      device_revoked:'Доступ отозван',login_succeeded:'Вход выполнен',login_rejected:'Вход отклонён',
      registration_rejected:'Ключ не прошёл проверку',auth_rejected:'Отклонённая попытка входа',logout:'Выход'};
    reconcileKeyedRows($('audit'), data.events, auditViews, () => {
      const element=node('p'); element.className='audit-row'; return {element};
    }, (view,row)=>set(view.element,`${date(row.created_at)} · ${names[row.event]||'Событие входа'}${row.label?' · '+row.label:''}${row.ip?' · '+row.ip:''}`));
    if (invitation && invitation.expires_at*1000 <= Date.now()) {
      $('invite-expiry').textContent='Приглашение истекло. Можно создать новое.';
    }
  } catch (error) { $('device-status').textContent=error.message||'Нет соединения.'; }
  finally { loading=false; }
}
$('create-invite').onclick=()=>action(async()=>{
  $('create-invite').disabled=true;
  try {
    if (invitation) await api(`/api/devices/invitations/${invitation.id}/cancel`,{});
    invitation=await api('/api/devices/invitations',{});
    if (imageURL) URL.revokeObjectURL(imageURL);
    const bytes=Uint8Array.from(atob(invitation.qr),c=>c.charCodeAt(0));
    imageURL=URL.createObjectURL(new Blob([bytes],{type:'image/png'}));
    $('invite-qr').src=imageURL; $('public-link').href=invitation.url;
    $('public-link').textContent=new URL(invitation.url).origin;
    $('invite-code').textContent=invitation.code.match(/.{1,4}/g).join(' ');
    $('invite-expiry').textContent=`Действует до ${new Date(invitation.expires_at*1000).toLocaleTimeString('ru-RU')}. После подключения сверьте код на обоих экранах.`;
    $('invite').hidden=false;
  } finally { $('create-invite').disabled=false; }
});
$('cancel-invite').onclick=()=>action(async()=>{
  if (invitation) await api(`/api/devices/invitations/${invitation.id}/cancel`,{});
  invitation=null; $('invite').hidden=true;
});
refresh(); setInterval(refresh,3000);
