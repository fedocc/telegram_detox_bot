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
  assert.match(worker, /url\.pathname\.startsWith\('\/api\/'\)/);
  assert.doesNotMatch(worker, /SHELL[^;]*\/api\//s);
  assert.match(worker, /SHELL\.has\(url\.pathname\)/);
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
  assert.match(page, /rel="apple-touch-icon" sizes="180x180" href="\/static\/app-icon-180\.png"/);
  assert.match(style, /@media \(max-width:768px\)/);
  assert.match(style, /safe-area-inset-bottom/);
  assert.doesNotMatch(style, /min-width:780px/);
});
