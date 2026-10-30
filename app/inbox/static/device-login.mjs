const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let invitation = fragment.get('pair') || '', busy = false, timer;
if (location.hash) history.replaceState(null, '', location.pathname + location.search);
if (!/^[A-Za-z0-9_-]{43}$/.test(invitation)) invitation = '';
const decode = value => Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')
  + '='.repeat((4 - value.length % 4) % 4)), c => c.charCodeAt(0));
const encode = value => btoa(String.fromCharCode(...new Uint8Array(value)))
  .replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');

function destination() {
  const value = new URLSearchParams(location.search).get('next') || '/';
  if (!value.startsWith('/') || value.startsWith('//')) return '/';
  const url = new URL(value, location.origin);
  return url.origin === location.origin ? url.pathname + url.search : '/';
}

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {
    method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить вход.');
  return data;
}

function credentialJSON(credential) {
  const response = {}, value = credential.response;
  for (const key of ['clientDataJSON', 'attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
    if (value[key]) response[key] = encode(value[key]);
  }
  if (value.getTransports) response.transports = value.getTransports();
  return {id: credential.id, rawId: encode(credential.rawId), type: credential.type, response,
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment};
}

async function action(work) {
  if (busy) return;
  busy = true; clearTimeout(timer); $('auth-status').textContent = '';
  $('register').disabled = $('login').disabled = true;
  try { await work(); }
  catch (error) {
    $('auth-status').textContent = error.name === 'NotAllowedError'
      ? 'Подтверждение отменено. Можно повторить.' : error.message || 'Нет соединения с сайтом.';
  } finally {
    busy = false; $('register').disabled = $('login').disabled = false;
  }
}

async function refresh() {
  clearTimeout(timer);
  if (busy) return;
  try {
    const state = await request('/auth/status');
    if (state.authenticated) { location.replace(destination()); return; }
    $('enrollment').hidden = !['new', 'expired', 'revoked'].includes(state.state);
    $('pending').hidden = state.state !== 'pending';
    $('login').hidden = state.state !== 'approved';
    $('retry').hidden = true;
    if (state.state === 'new') {
      $('invitation-field').hidden = Boolean(invitation);
      $('description').textContent = invitation ? 'Приглашение получено. Создайте ключ доступа для этого браузера.'
        : 'На Mac откройте «Доверенные устройства» и создайте приглашение.';
    } else if (state.state === 'pending') {
      $('heading').textContent = 'Подтвердите на Mac';
      $('confirmation-code').textContent = state.code.match(/.{1,3}/g).join(' ');
      timer = setTimeout(refresh, 3000);
    } else if (state.state === 'approved') {
      $('heading').textContent = 'Можно входить';
      $('description').textContent = `${state.label} подтверждён. Используйте ключ доступа.`;
    } else {
      $('heading').textContent = 'Подключите заново';
      $('invitation-field').hidden = Boolean(invitation);
      $('description').textContent = 'Доверие отозвано или срок подключения истёк. Создайте новое приглашение на Mac.';
    }
  } catch (_) {
    $('auth-status').textContent = 'Нет соединения с сайтом. Проверьте интернет и повторите.';
    $('retry').hidden = false;
  }
}

$('register').onclick = () => action(async () => {
  if (!navigator.credentials || !window.PublicKeyCredential) throw new Error('Откройте сайт в Safari по HTTPS.');
  const code = invitation || $('invitation').value.replace(/[\s-]/g, '');
  if (!/^(?:[A-Za-z0-9_-]{43}|[0-9]{12})$/.test(code)) throw new Error('Введите 12 цифр приглашения с Mac.');
  const ceremony = await request('/auth/register/options', {invitation: code, label: $('device-label').value.trim()});
  const options = ceremony.options;
  options.challenge = decode(options.challenge); options.user.id = decode(options.user.id);
  options.excludeCredentials = (options.excludeCredentials || []).map(c => ({...c, id: decode(c.id)}));
  const credential = await navigator.credentials.create({publicKey: options});
  await request('/auth/register/finish', {ceremony: ceremony.ceremony, credential: credentialJSON(credential)});
  invitation = ''; $('invitation').value = '';
  busy = false; await refresh();
});
$('login').onclick = () => action(async () => {
  const ceremony = await request('/auth/login/options', {}), options = ceremony.options;
  options.challenge = decode(options.challenge);
  options.allowCredentials = options.allowCredentials.map(c => ({...c, id: decode(c.id)}));
  const credential = await navigator.credentials.get({publicKey: options});
  await request('/auth/login/finish', {ceremony: ceremony.ceremony, credential: credentialJSON(credential)});
  location.replace(destination());
});
$('retry').onclick = refresh;
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
refresh();
