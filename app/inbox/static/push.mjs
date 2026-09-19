function applicationKey(value) {
  const padded = value + '='.repeat((4 - value.length % 4) % 4);
  const bytes = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(bytes, character => character.charCodeAt(0));
}

export function createPushController({
  registration, request, notificationApi, badgeNavigator,
  render = () => {},
}) {
  let status = {available: false, configured: false, enabled: false, busy: false,
    denied: false, error: ''};
  const show = changes => { status = {...status, ...changes}; render(status); };
  const supported = Boolean(registration?.pushManager && notificationApi);

  async function refresh() {
    if (!supported) { show({available: false}); return; }
    try {
      const config = await request('/api/push/config');
      const subscription = await registration.pushManager.getSubscription();
      show({available: true, configured: config.configured === true,
        publicKey: config.public_key || '', enabled: Boolean(subscription),
        denied: notificationApi.permission === 'denied', error: ''});
    } catch (_) { show({available: false, error: 'Push сейчас недоступен.'}); }
  }

  async function enable() {
    if (!supported || status.busy || !status.configured || !status.publicKey) return;
    show({busy: true, error: ''});
    try {
      const permission = await notificationApi.requestPermission();
      if (permission !== 'granted') {
        show({busy: false, denied: permission === 'denied', enabled: false}); return;
      }
      let subscription = await registration.pushManager.getSubscription();
      subscription ||= await registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: applicationKey(status.publicKey),
      });
      await request('/api/push/subscriptions', {
        subscription: subscription.toJSON(), device_label: 'iPhone PWA',
      });
      show({busy: false, denied: false, enabled: true});
    } catch (_) { show({busy: false, error: 'Не удалось включить уведомления.'}); }
  }

  async function disable() {
    if (!supported || status.busy) return;
    show({busy: true, error: ''});
    try {
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        await request('/api/push/unsubscribe', {subscription: subscription.toJSON()});
        await subscription.unsubscribe();
      }
      await setBadge(0); show({busy: false, enabled: false});
    } catch (_) { show({busy: false, error: 'Не удалось выключить уведомления.'}); }
  }

  async function setBadge(count) {
    try {
      if (count > 0 && typeof badgeNavigator?.setAppBadge === 'function') {
        await badgeNavigator.setAppBadge(count);
      } else if (typeof badgeNavigator?.clearAppBadge === 'function') {
        await badgeNavigator.clearAppBadge();
      }
    } catch (_) {}
  }

  return {refresh, enable, disable, setBadge};
}
