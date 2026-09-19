import assert from 'node:assert/strict';
import test from 'node:test';
import {createPushController} from '../app/inbox/static/push.mjs';

function fixture({permission='default'}={}) {
  const calls=[],renders=[]; let current=null,badge=null;
  const subscription={toJSON:()=>({endpoint:'https://push.test/x',expirationTime:null,
    keys:{p256dh:'p',auth:'a'}}),unsubscribe:async()=>{current=null;return true;}};
  const registration={pushManager:{getSubscription:async()=>current,
    subscribe:async options=>{assert.equal(options.userVisibleOnly,true);current=subscription;return current;}}};
  const notificationApi={permission,requestPermission:async()=>permission};
  const controller=createPushController({registration,notificationApi,
    badgeNavigator:{setAppBadge:async value=>{badge=value;},clearAppBadge:async()=>{badge=0;}},
    request:async(path,body)=>{calls.push([path,body]);if(path==='/api/push/config')return{configured:true,public_key:'BHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg'};return{enabled:true};},
    render:value=>renders.push(value)});
  return{controller,calls,renders,setPermission:value=>{notificationApi.permission=value;notificationApi.requestPermission=async()=>value;},badge:()=>badge};
}

test('permission is requested only by explicit enable action and subscription is persisted',async()=>{
  const f=fixture();await f.controller.refresh();
  assert.deepEqual(f.calls.map(value=>value[0]),['/api/push/config']);
  f.setPermission('granted');await f.controller.enable();
  assert.equal(f.calls.at(-1)[0],'/api/push/subscriptions');assert.equal(f.renders.at(-1).enabled,true);
});

test('denied permission is reported without subscription retries',async()=>{
  const f=fixture({permission:'denied'});await f.controller.refresh();await f.controller.enable();
  assert.equal(f.renders.at(-1).denied,true);
  assert.equal(f.calls.filter(value=>value[0]==='/api/push/subscriptions').length,0);
});

test('badge updates and clears without affecting push state',async()=>{
  const f=fixture();await f.controller.setBadge(7);assert.equal(f.badge(),7);
  await f.controller.setBadge(0);assert.equal(f.badge(),0);
});
