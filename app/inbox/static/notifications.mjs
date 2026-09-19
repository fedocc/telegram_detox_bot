export function notificationMode({enabled = false, mute_until = null} = {}) {
  if (!enabled) return 'disable';
  return Number.isFinite(mute_until) ? 'pause' : 'enable';
}

export function createNotificationToggle({request, render}) {
  const durations = new Set([600, 1800, 3600, 10800, 21600, 43200]);
  let status={enabled:false,effective_enabled:false,mute_until:null,permission:'unknown'};
  let available=false, busy=false, csrf='';
  const show=(error='')=>render({...status,available,busy,error});
  function accepted(raw) {
    if (typeof raw?.csrf === 'string') csrf=raw.csrf;
    const enabled=raw?.enabled===true;
    return {
      enabled,
      effective_enabled:raw?.effective_enabled===true,
      mute_until:Number.isFinite(raw?.mute_until)?raw.mute_until:null,
      permission:typeof raw?.permission==='string'?raw.permission:'unknown',
    };
  }
  async function refresh() {
    if(busy)return; busy=true;
    try { status=accepted(await request('/status')); available=true; busy=false; show(); }
    catch(_){available=false;busy=false;show();}
  }
  async function action(value) {
    const seconds=Number(value);
    if(!available||busy||!(value==='enable'||value==='disable'||durations.has(seconds)))return;
    const previous=status; busy=true; show();
    try {
      const path=value==='enable'?'/enable':value==='disable'?'/disable':'/snooze';
      status=accepted(await request(path,csrf,path==='/snooze'?{seconds}:{}));
      busy=false;show();
    } catch(_){status=previous;busy=false;show('Не удалось изменить');}
  }
  async function toggle(){return action(status.enabled?'disable':'enable');}
  async function conversationOpened(id) {
    if(!available||!csrf||!/^[a-f0-9]{32}$/.test(id||''))return;
    try { await request('/conversation-opened',csrf,{conversation_id:id}); } catch(_) {}
  }
  return {refresh,toggle,action,conversationOpened};
}

export function linkedConversation(search, rows) {
  const value = new URLSearchParams(search).get('conversation');
  return /^[a-f0-9]{32}$/.test(value || '') && rows.some(row => row.id === value) ? value : null;
}
