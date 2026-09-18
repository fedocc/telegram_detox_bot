export function createNotificationToggle({request, render}) {
  let status={enabled:false,effective_enabled:false,mute_until:null}, available=false, busy=false, csrf='';
  const show=(error='')=>render({...status,available,busy,error});
  async function refresh() {
    if(busy)return; busy=true;
    try { status=await request('/status'); csrf=status.csrf; available=true; busy=false; show(); }
    catch(_){available=false;busy=false;show();}
  }
  async function action(value) {
    if(!available||busy)return; const previous=status; busy=true; show();
    try {
      const path=value==='enable'?'/enable':value==='disable'?'/disable':'/snooze';
      status=await request(path,csrf,path==='/snooze'?{seconds:Number(value)}:{});
      busy=false;show();
    } catch(_){status=previous;busy=false;show('Не удалось изменить');}
  }
  async function toggle(){return action(status.enabled?'disable':'enable');}
  async function conversationOpened(id) {
    if(!available||!csrf)return;
    try { await request('/conversation-opened',csrf,{conversation_id:id}); } catch(_) {}
  }
  return {refresh,toggle,action,conversationOpened};
}

export function linkedConversation(search, rows) {
  const value = new URLSearchParams(search).get('conversation');
  return /^[a-f0-9]{32}$/.test(value || '') && rows.some(row => row.id === value) ? value : null;
}
