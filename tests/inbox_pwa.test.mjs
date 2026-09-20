import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../app/inbox/static/', import.meta.url);

test('manifest is standalone and uses only local icons', async () => {
  const manifest = JSON.parse(await readFile(new URL('manifest.webmanifest', root), 'utf8'));
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.start_url, '/');
  assert.equal(manifest.scope, '/');
  assert.deepEqual(manifest.icons.map(icon => [icon.src, icon.sizes, icon.type]), [
    ['/static/app-icon-180.png', '180x180', 'image/png'],
    ['/static/app-icon-512.png', '512x512', 'image/png'],
  ]);
  assert.ok(manifest.icons.every(icon => icon.purpose.includes('maskable')));
});

test('PWA PNG icons have the declared dimensions and stay compact', async () => {
  for (const size of [180, 512]) {
    const png = await readFile(new URL(`app-icon-${size}.png`, root));
    assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    assert.equal(png.readUInt32BE(16), size);
    assert.equal(png.readUInt32BE(20), size);
    assert.ok(png.length < 64 * 1024, `${size}px icon should be smaller than 64 KiB`);
  }
});

test('service worker explicitly bypasses every API request', async () => {
  const worker = await readFile(new URL('sw.js', root), 'utf8');
  assert.match(worker, /telegram-detox-shell-v10/);
  for (const asset of ['style.css', 'app.js', 'ui.mjs', 'notifications.mjs']) {
    assert.match(worker, new RegExp(`/static/${asset.replace('.', '\\.')}\\?v=10`));
  }
  assert.match(worker, /url\.pathname\.startsWith\('\/api\/'\)/);
  assert.doesNotMatch(worker, /SHELL[^;]*\/api\//s);
  assert.match(worker, /SHELL\.has\(shellKey\)/);
  assert.match(worker, /\/static\/app-icon-180\.png/);
  assert.match(worker, /\/static\/app-icon-512\.png/);
  assert.doesNotMatch(worker, /\/static\/app-icon\.svg/);
});

test('mobile shell declares safe-area and removes the legacy minimum width', async () => {
  const [page, style] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.match(page, /viewport-fit=cover/);
  assert.match(page, /manifest\.webmanifest/);
  assert.match(page, /style\.css\?v=10/);
  assert.match(page, /app\.js\?v=10/);
  assert.match(page, /rel="apple-touch-icon" sizes="180x180" href="\/static\/app-icon-180\.png"/);
  assert.match(style, /@media \(max-width:768px\)/);
  assert.match(style, /safe-area-inset-bottom/);
  assert.doesNotMatch(style, /min-width:780px/);
});

test('service worker push is visible, grouped and accepts only conversation deep links', async () => {
  const worker = await readFile(new URL('sw.js', root), 'utf8');
  assert.match(worker, /addEventListener\('push'/);
  assert.match(worker, /showNotification/);
  assert.match(worker, /conversation:\$\{conversation\}/);
  assert.match(worker, /addEventListener\('notificationclick'/);
  assert.doesNotMatch(worker, /payload\.url/);
});

test('mobile dock has safe-area, glass fallback and compact pinned/search layouts', async () => {
  const [page, style] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  for (const id of ['mobile-dock', 'mobile-library', 'mobile-push', 'push-panel']) {
    assert.match(page, new RegExp(`id="${id}"`));
  }
  assert.match(style, /@supports \(\(backdrop-filter/);
  assert.match(style, /prefers-reduced-transparency:reduce/);
  assert.match(style, /prefers-reduced-motion:no-preference/);
  assert.match(style, /#pinned-list \{ max-height:92px/);
  assert.match(style, /#search-input \{ min-height:44px/);
});

test('history polling fast paths and pagination preserve existing message nodes', async () => {
  const app = await readFile(new URL('app.js', root), 'utf8');
  assert.match(app, /if \(!initial && !messagesChanged\(signatures, unique\)\) return false/);
  assert.match(app, /if \(prepend && !initial\) return prependMessagePage\(messages, key\)/);
  assert.match(app, /appendOnlyMessages\(signatures, unique\)/);
  assert.match(app, /return incrementalMessagePage\(unique, key\)/);
  assert.match(app, /const additions = newPageMessages\(existingIds, messages\)/);
  assert.match(app, /insertNewNodesInOrder\(list, desired, messageNodes, inserted\)/);
  assert.match(app, /restoreScrollAnchor\(list, messageNodes, anchor, oldHeight\)/);
  assert.match(app, /document\.elementFromPoint/);
  assert.doesNotMatch(app, /\.\.\.list\.querySelectorAll\('\[data-id\]'\)/);
});

test('ordinary opens settle at bottom while explicit message navigation keeps exact focus', async () => {
  const app = await readFile(new URL('app.js', root), 'utf8');
  assert.match(app, /await Promise\.all\(\[loadMessages\(key\), loadPins\('inbox', key\)\]\)[\s\S]*?if \(messageId\) await focusMessage\(messageId,[\s\S]*?else settleScrollBottom/);
  assert.match(app, /await Promise\.all\(\[loadLibrary\(key, null, !changed\), loadPins\('library', key\)\]\)[\s\S]*?if \(messageId\) await focusMessage\(messageId,[\s\S]*?else settleScrollBottom/);
  assert.match(app, /item\.node\.scrollIntoView\(\{block: 'center'\}\)/);
  assert.doesNotMatch(app, /trigger\.node\.scrollIntoView/);
  assert.match(app, /maintainMessageViewport\(list, messageNodes, \{atBottom, anchor, oldHeight\}\)/);
});

test('pins begin collapsed and data refresh preserves an explicit expansion', async () => {
  const [page, app] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
  ]);
  assert.match(page, /id="pinned-toggle"[^>]*aria-expanded="false"/);
  assert.match(page, /id="pinned-list" hidden/);
  assert.match(app, /resetConversationSurface\(\)[\s\S]*?pinned-toggle'\)\.setAttribute\('aria-expanded', 'false'\)/);
  assert.match(app, /const expanded = \$\('pinned-toggle'\)\.getAttribute\('aria-expanded'\) === 'true'/);
  assert.match(app, /\$\('pinned-list'\)\.hidden = !expanded/);
});

test('desktop Home is navigation only and mobile back keeps its existing action', async () => {
  const [page, app, style] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.doesNotMatch(page, />активные</);
  assert.match(page, /<button id="home"[^>]*aria-label="На главный экран"[^>]*>[\s\S]*?<svg/);
  assert.match(app, /\$\('home'\)\.onclick = \(\) => deselect\(\)/);
  assert.match(app, /\$\('mobile-back'\)\.onclick = \(\) => deselect\(\)/);
  assert.match(app, /const url = selected \? deepLinkFor[\s\S]*?: '\/'/);
  assert.match(app, /if \(removeCurrent && previousMode === 'inbox'\)/);
  assert.doesNotMatch(app, /\$\('home'\)\.onclick\s*=\s*\(\)\s*=>\s*closeConversation/);
  assert.match(style, /@media \(max-width:768px\)[\s\S]*?\.list-actions \{ display:none \}/);
});

test('digest history navigation is desktop-only, ordered before Home and route-aware', async () => {
  const [page, app, style] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  const historyIndex = page.indexOf('id="digest-history-button"');
  const homeIndex = page.indexOf('id="home"');
  assert.ok(historyIndex > 0 && historyIndex < homeIndex);
  assert.match(page, /id="digest-history-button"[^>]*aria-label="История сводок"[\s\S]*?<svg/);
  assert.match(page, /id="digest-history"[^>]*hidden/);
  assert.match(app, /\$\('digest-history-button'\)\.onclick = \(\) => showDigestHistory\(\)/);
  assert.match(app, /api\('\/api\/digest\/history'\)/);
  assert.match(app, /history\.pushState\(null, '', '\/\?view=digests'\)/);
  assert.match(app, /if \(isDigestHistoryRoute\(location\.search\)\)/);
  assert.match(app, /prepareSelection[\s\S]*?appView = 'home'/);
  assert.match(app, /renderDigestHistory/);
  assert.match(style, /@media \(max-width:768px\)[\s\S]*?\.list-actions \{ display:none \}/);
});

test('desktop and mobile chat back share navigation-only deselection', async () => {
  const [page, app, style] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.match(page, /id="mobile-back" class="chat-back"[^>]*aria-label="К списку"/);
  assert.match(style, /\.chat-back \{ display:block/);
  assert.match(app, /\$\('mobile-back'\)\.onclick = \(\) => deselect\(\)/);
  assert.doesNotMatch(app, /\$\('mobile-back'\)\.onclick\s*=\s*\(\)\s*=>\s*closeConversation/);
  assert.match(app, /if \(removeCurrent && previousMode === 'inbox'\)/);
});

test('badge reconciles from canonical server state on foreground lifecycle events', async () => {
  const [app, worker] = await Promise.all([
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('sw.js', root), 'utf8'),
  ]);
  assert.match(app, /async function syncBadge\(\)[\s\S]*?api\('\/api\/badge'\)[\s\S]*?setBadge\(canonicalBadge\(result\.badge\)\)/);
  assert.match(app, /addEventListener\('pageshow', \(\) => syncBadge\(\)\)/);
  assert.match(app, /addEventListener\('focus', \(\) => syncBadge\(\)\)/);
  assert.match(app, /visibilityState === 'visible'\) syncBadge\(\)/);
  assert.match(app, /await pushController\.refresh\(\);[\s\S]*?await syncBadge\(\)/);
  assert.match(app, /prepareSelection\('inbox', key\);[\s\S]*?syncBadge\(\)/);
  assert.match(worker, /showNotification/);
  assert.match(worker, /clearAppBadge/);
  assert.doesNotMatch(worker, /silent\s*:/);
});

test('paste and drag reuse the attachment path without intercepting ordinary text', async () => {
  const [app, page, style] = await Promise.all([
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.match(app, /addEventListener\('paste', event => \{/);
  assert.match(app, /const file = clipboardFile\(event\.clipboardData\)/);
  assert.match(app, /if \(file\) \{[\s\S]*?event\.preventDefault\(\); chooseFile\(file\)/);
  assert.match(app, /if \(localImageClipboardUri\(event\.clipboardData\)\) \{[\s\S]*?event\.preventDefault\(\)/);
  assert.match(app, /function chooseFile\(file\)[\s\S]*?uploadWithinLimit\(file, uploadMax\)/);
  assert.match(app, /URL\.createObjectURL\(file\)/);
  assert.match(page, /id="attachment-preview"/);
  assert.match(app, /createFileDragTracker/);
  assert.match(app, /addEventListener\('dragenter'/);
  assert.match(app, /addEventListener\('dragover'/);
  assert.match(app, /addEventListener\('dragleave'/);
  assert.match(app, /addEventListener\('drop'/);
  assert.match(app, /window\.addEventListener\('dragend', \(\) => fileDrag\.reset\(\)\)/);
  assert.match(style, /#drop-zone \{[^}]*pointer-events:none/);
});

test('service messages use a separate control-free system row', async () => {
  const [app, style] = await Promise.all([
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.match(app, /if \(message\.system\)/);
  assert.match(app, /node\('div', 'system-message', message\.system\)/);
  assert.match(style, /\.system-message \{/);
});

test('notification UI exposes only compact ON PAUSE OFF primary controls', async () => {
  const [page, app] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
  ]);
  const primary = [...page.matchAll(/data-notification-action="([^"]+)"[^>]*>([^<]+)</g)]
    .map(match => [match[1], match[2]]);
  assert.deepEqual(primary,[['enable','ON'],['disable','OFF']]);
  assert.match(page, /id="notifications-pause"[\s\S]*?>PAUSE<\/button>/);
  assert.match(page, /data-notification-duration="600">10м/);
  assert.match(page, /data-notification-duration="43200">12ч/);
  assert.doesNotMatch(page, /Включить сейчас|Выключить полностью|● Включены/);
  assert.match(app, /notificationMode\(\{enabled, mute_until\}\)/);
  assert.match(app, /nextAuxiliaryPanel\(auxiliaryPanel, panel\)/);
});

test('stickers render as media or an explicit fallback outside ordinary bubbles', async () => {
  const [app, style] = await Promise.all([
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ]);
  assert.match(app, /media\.sticker_format === 'static'/);
  assert.match(app, /media\.sticker_format === 'video'/);
  assert.match(app, /Не удалось загрузить стикер/);
  assert.match(app, /'\[Стикер\]'/);
  assert.match(app, /video\.muted = true/);
  assert.match(app, /video\.loop = true/);
  assert.match(app, /stickerObserver\?\.observe\(video\)/);
  assert.match(style, /\.sticker-bubble \{/);
  assert.match(style, /\.sticker-media \{/);
});

test('custom emoji and morning digest render inline without replacing stable message nodes', async () => {
  const app = await readFile(new URL('app.js', root), 'utf8');
  const style = await readFile(new URL('style.css', root), 'utf8');
  const page = await readFile(new URL('index.html', root), 'utf8');
  assert.match(app, /custom\?\.available && custom\.format === 'static'/);
  assert.match(app, /custom\?\.available && custom\.format === 'video'/);
  assert.match(app, /image\.alt = text/);
  assert.match(app, /video\.replaceWith\(document\.createTextNode\(text\)\)/);
  assert.match(app, /stickerObserver\?\.observe\(video\)/);
  assert.match(style, /\.custom-emoji \{/);
  assert.match(page, /id="morning-digest"/);
  assert.match(app, /digestTitle\(digest\.period_end\)/);
  assert.match(app, /'digest-dismiss', '×'/);
  assert.match(app, /dismissDigest\(storage, digest\)/);
  assert.doesNotMatch(app, /☀️ Утро/);
  assert.match(app, /\/?library=|item\.links\?\.\[0\]/);
  assert.match(app, /incrementalMessagePage/);
});

test('all frontend shell references use the same cache version', async () => {
  const [page, app, worker] = await Promise.all([
    readFile(new URL('index.html', root), 'utf8'),
    readFile(new URL('app.js', root), 'utf8'),
    readFile(new URL('sw.js', root), 'utf8'),
  ]);
  for (const source of [page, app, worker]) assert.doesNotMatch(source, /\?v=(?:[1-9])(?:\D|$)|shell-v[1-9](?:\D|$)/);
  assert.match(worker, /telegram-detox-shell-v10/);
  assert.match(page, /app\.js\?v=10/);
  assert.match(app, /ui\.mjs\?v=10/);
});

test('aggregate reactions render compactly and message reconciliation replaces only changed nodes', async () => {
  const app = await readFile(new URL('app.js', root), 'utf8');
  const style = await readFile(new URL('style.css', root), 'utf8');
  assert.match(app, /function reactionsNode\(reactions\)/);
  assert.match(app, /reactionIcon\(reaction\)/);
  assert.match(app, /reaction\?\.emoji \|\| '◉'/);
  assert.match(app, /'reaction-emoji', reactionEmojiPresentation\(fallback\)/);
  assert.match(app, /'reaction-count', String\(Number\(reaction\.count\)\)/);
  assert.match(app, /const meta = node\('div', 'message-meta'\)/);
  assert.match(app, /meta\.append\(reactions, timestamp\)/);
  assert.match(app, /else bubble\.append\(timestamp\)/);
  assert.match(app, /for \(const reaction of reactions \|\| \[\]\)/);
  assert.match(app, /custom\?\.available && custom\.format === 'static'/);
  assert.match(app, /custom\?\.available && custom\.format === 'video'/);
  assert.match(app, /else if \(existing\.signature !== signature\)/);
  assert.match(app, /existing\.node\.replaceWith\(element\)/);
  assert.match(style, /\.message-reactions \{/);
  assert.match(style, /\.reaction-emoji \{[^}]*Apple Color Emoji/);
  assert.match(style, /\.reaction-count \{/);
  assert.match(style, /\.message-meta \.timestamp \{[^}]*float:none/);
  assert.match(style, /gap:6px/);
});
