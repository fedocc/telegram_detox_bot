export function createNotificationToggle({request, render}) {
  let enabled = false, available = false, busy = false, csrf = '';
  const show = (error = '', permission = '') => render({enabled, available, busy, error, permission});
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const status = await request('/status');
      enabled = status.enabled; csrf = status.csrf; available = true;
      busy = false; show('', status.permission);
    } catch (_) { available = false; busy = false; show(); }
  }
  async function toggle() {
    if (!available || busy) return;
    const previous = enabled;
    enabled = !previous; busy = true; show();
    try {
      const status = await request(enabled ? '/enable' : '/disable', csrf);
      enabled = status.enabled; busy = false; show('', status.permission);
    } catch (_) { enabled = previous; busy = false; show('Не удалось изменить'); }
  }
  return {refresh, toggle};
}

// A notification click is an explicit open intent. Feed/list/status requests are not.
export function linkedConversation(search, rows) {
  const value = new URLSearchParams(search).get('conversation');
  return /^[a-f0-9]{32}$/.test(value || '') && rows.some(row => row.id === value) ? value : null;
}
