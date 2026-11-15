import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, rm, rmdir, writeFile } from 'node:fs/promises';
import { createServer, request as sendRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';

import { createWebsite, settings } from '../server.mjs';
import { APK_PATH, createAndroidDownloads, QR_PATH } from '../../AndroidAppServer/server.mjs';
import qrcode from '../../AndroidAppServer/vendor/qrcode-generator-2.0.4.mjs';

async function bind(server, t) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => {
    server.close(resolve);
    server.closeAllConnections();
  }));
  return `http://127.0.0.1:${server.address().port}`;
}

function reply(res, status, data, headers = {}) {
  res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  res.end(JSON.stringify(data));
}

function requestWithHost(url, method, headers, body) {
  return new Promise((resolve, reject) => {
    const outgoing = sendRequest(url, { method, headers, signal: AbortSignal.timeout(5000) }, async res => {
      try {
        const chunks = [];
        for await (const chunk of res) chunks.push(chunk);
        const responseHeaders = new Headers();
        for (const [name, value] of Object.entries(res.headers)) {
          for (const item of [value].flat()) responseHeaders.append(name, item);
        }
        resolve(new Response(chunks.length ? Buffer.concat(chunks) : null, {
          status: res.statusCode, headers: responseHeaders,
        }));
      } catch (error) {
        reject(error);
      }
    });
    outgoing.on('error', reject);
    outgoing.end(body);
  });
}

async function fixture(t, handler, { env = {}, ...options } = {}) {
  const requests = [];
  const upstream = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const input = { path: req.url, method: req.method, headers: req.headers, body: raw.toString('utf8'), raw };
    requests.push(input);
    try {
      await handler(input, res);
    } catch (err) {
      t.diagnostic(err.message);
      reply(res, 500, { error: { message: 'test fixture failure' } });
    }
  });
  const config = settings({ ...env, USER_WEB_UIM_URL: await bind(upstream, t) });
  const address = await bind(createWebsite(config, options), t);
  if (!env.USER_WEB_PUBLIC_URL) config.publicUrl.port = new URL(address).port;
  const origin = config.publicUrl.origin;
  function request(path, { method = 'GET', cookie, data, upload, headers = {}, sameOrigin = true } = {}) {
    if (cookie) headers.Cookie = cookie;
    if (upload !== undefined) headers['Content-Type'] = 'application/octet-stream';
    else if (data !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && sameOrigin) headers.Origin = origin;
    const body = upload ?? (data === undefined ? undefined : JSON.stringify(data));
    if (origin !== address) return requestWithHost(address + path, method, { ...headers, Host: config.publicUrl.host }, body);
    return fetch(address + path, { method, headers, body, redirect: 'manual', signal: AbortSignal.timeout(5000) });
  }
  return { origin, request, requests };
}

const pair = (access, refresh) => ({ access_token: access, refresh_token: refresh, token_type: 'Bearer' });
const cookiesFrom = response => response.headers.getSetCookie().map(header => header.split(';')[0]);

test('serves both Markdown modules directly as JavaScript', async t => {
  const { request, requests } = await fixture(t, () => assert.fail('static files should not contact upstream'));
  for (const path of ['/markdown.mjs', '/vendor/markdown-it-15.0.3.mjs']) {
    const response = await request(path);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), 'text/javascript; charset=utf-8');
    assert.match(await response.text(), /export /);
  }
  assert.deepEqual(requests, []);
});

test('the privacy statement and its links are accessible before sign-in', async t => {
  const { request, requests } = await fixture(t, () => assert.fail('public privacy assets do not use an account API'));
  const privacy = await request('/privacy');
  assert.equal(privacy.status, 200);
  assert.equal(privacy.headers.has('set-cookie'), false);
  assert.match(await privacy.text(), /<h1>隐私说明<\/h1>/);
  assert.match((await request('/privacy.css')).headers.get('content-type'), /^text\/css/);
  assert.match((await request('/auth/password.js')).headers.get('content-type'), /^text\/javascript/);
  const home = await (await request('/')).text();
  assert.match(home, /class="landing-privacy" href="\/privacy"/);
  assert.match(home, /class="dialog-footer"><a href="\/privacy"/);
  assert.match(await (await request('/admin')).text(), /href="\/privacy"/);
  assert.deepEqual(requests, []);
});

test('serves the Android installer and an HTTPS download QR without a website session', async t => {
  const folder = await mkdtemp(join(tmpdir(), 'campus-apk-test-'));
  const apk = join(folder, 'fixture.apk');
  t.after(async () => { await rm(apk, { force: true }); await rmdir(folder); });
  const release = Buffer.from([80, 75, 3, 4, 0, 240]);
  await writeFile(apk, release);
  const androidUrl = await bind(createAndroidDownloads({ apk }), t);
  const { origin, request, requests } = await fixture(t, () => assert.fail('downloads do not use the account API'), {
    androidUrl, env: { USER_WEB_PUBLIC_URL: 'https://campus.example.test:8443' },
  });
  const raw = await request(APK_PATH);
  assert.equal(raw.status, 200);
  assert.equal(raw.headers.get('content-type'), 'application/vnd.android.package-archive');
  assert.equal(raw.headers.get('content-disposition'), 'attachment; filename="cuc-campus-calendar.apk"');
  assert.deepEqual(Buffer.from(await raw.arrayBuffer()), release);
  const head = await request(APK_PATH, { method: 'HEAD' });
  assert.equal(head.headers.get('content-length'), String(release.length));
  assert.equal(await head.text(), '');

  const qr = await request(QR_PATH);
  const expected = qrcode(0, 'M');
  expected.addData(origin + APK_PATH);
  expected.make();
  assert.equal(qr.headers.get('content-type'), 'image/svg+xml');
  assert.equal(await qr.text(), expected.createSvgTag({ cellSize: 5, margin: 20 }));
  assert.equal((await request(QR_PATH + '?origin=https://other.example.test')).status, 404);
  assert.equal((await request(APK_PATH, { method: 'POST', data: {} })).status, 405);
  assert.deepEqual(requests, []);
  await rm(apk);
  assert.equal((await request(APK_PATH)).status, 503);
});

test('retains the static avatar fallback with no bundled private media', async t => {
  const { request, requests } = await fixture(t, () => assert.fail('assets should not contact upstream'));
  assert.equal((await request('/campus.svg')).status, 200);
  assert.equal((await request('/campus-avatar.js')).status, 200);
  assert.equal((await request('/live2d/model/seethrough_output_1.model3.json')).status, 404);
  assert.deepEqual(requests, []);
});

async function login(request, email, previous) {
  const response = await request('/v1/auth/login', { method: 'POST', data: { email, password: 'a test password' }, cookie: previous });
  assert.equal(response.status, 204);
  assert.match(response.headers.get('set-cookie'), /HttpOnly; SameSite=Lax/);
  assert.doesNotMatch(response.headers.get('set-cookie'), /Max-Age/i);
  assert.equal(await response.text(), '');
  const [cookie] = cookiesFrom(response);
  const account = await request('/v1/me', { cookie });
  assert.equal(account.status, 200);
  const context = account.headers.get('x-website-context');
  assert.ok(context);
  return { cookie, context };
}

test('keeps tokens private and rotates a browser session once for concurrent requests', async t => {
  let expired = false;
  let refreshes = 0;
  const { request, requests } = await fixture(t, async (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/auth/refresh') {
      refreshes++;
      assert.deepEqual(JSON.parse(input.body), { refresh_token: 'refresh_first' });
      await delay(40);
      return reply(res, 200, pair('access_second', 'refresh_second'));
    }
    if (expired && input.headers.authorization !== 'Bearer access_second') {
      return reply(res, 401, { error: { code: 'unauthorized' } });
    }
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    return reply(res, 200, { items: [] });
  });
  assert.equal((await request('/admin/interfaces')).status, 404);
  assert.equal((await request('/v1/me', { headers: { Authorization: 'Bearer forged' } })).status, 401);
  assert.equal((await request('/v1/auth/login', { method: 'POST', data: {}, sameOrigin: false })).status, 403);
  assert.equal(requests.length, 0);

  const { cookie, context } = await login(request, 'alice@example.com');
  assert.doesNotMatch(cookie, /access_first|refresh_first/);
  expired = true;
  const access = () => request('/v1/instances', { cookie, headers: { 'X-Website-Context': context } });
  const responses = await Promise.all([access(), access(), access()]);
  assert.deepEqual(responses.map(res => res.status), [200, 200, 200]);
  assert.equal(refreshes, 1);
  assert.ok(requests.every(req => !req.headers.cookie && !req.headers['x-website-context']));
  assert.equal((await request('/v1/auth/refresh', { method: 'POST', data: {}, cookie })).status, 404);
});

test('remembered browsers restore after a website restart without exposing access tokens', async t => {
  const token = 'rm_' + 'x'.repeat(43);
  let revoked = false;
  let access = 'access_first';
  let restores = 0;
  const upstream = async (input, res) => {
    if (input.path === '/v1/auth/login') {
      assert.equal(JSON.parse(input.body).remember_me, true);
      return reply(res, 200, { ...pair(access, 'refresh_first'), remember_token: token, remember_expires_in: 2592000 });
    }
    if (input.path === '/v1/auth/remember') {
      restores++;
      assert.deepEqual(JSON.parse(input.body), { token });
      if (revoked) return reply(res, 401, { error: { code: 'invalid_credentials' } });
      await delay(40);
      access = `access_restored_${restores}`;
      return reply(res, 200, { access_token: access, remember_expires_in: 2591900 });
    }
    assert.equal(input.headers.authorization, `Bearer ${access}`);
    if (input.path === '/v1/auth/logout') {
      revoked = true;
      return res.writeHead(204).end();
    }
    return reply(res, 200, { id: 'alice' });
  };

  const first = await fixture(t, upstream);
  const signedIn = await first.request('/v1/auth/login', {
    method: 'POST', data: { email: 'alice@example.com', password: 'campus1a', remember_me: true },
  });
  assert.equal(signedIn.status, 204);
  assert.equal(await signedIn.text(), '');
  assert.match(signedIn.headers.get('set-cookie'), /HttpOnly; SameSite=Lax; Max-Age=2592000/);
  const [cookie] = cookiesFrom(signedIn);
  assert.doesNotMatch(cookie, /access_first|refresh_first/);
  const context = (await first.request('/v1/me', { cookie })).headers.get('x-website-context');
  assert.ok(context);

  const second = await fixture(t, upstream);
  const options = { cookie, headers: { 'X-Website-Context': context } };
  const [profile, instances] = await Promise.all([
    second.request('/v1/me', { cookie }), second.request('/v1/instances', options),
  ]);
  assert.deepEqual([profile.status, instances.status], [200, 200]);
  assert.equal(profile.headers.get('x-website-context'), context);
  assert.equal(restores, 1);
  assert.equal((await second.request('/v1/auth/remember', { method: 'POST', data: { token } })).status, 404);
  const loggedOut = await second.request('/v1/auth/logout', { ...options, method: 'POST' });
  assert.equal(loggedOut.status, 204);
  assert.match(loggedOut.headers.get('set-cookie'), /Max-Age=0/);
  assert.equal((await second.request('/v1/me', { cookie })).status, 401);
});

test('password change requests require the website session and relay cooldown timing', async t => {
  const path = '/v1/auth/password/change/request';
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    assert.equal(input.path, path);
    assert.deepEqual(JSON.parse(input.body), { old_password: 'campus1a' });
    assert.equal(input.headers.authorization, 'Bearer access_first');
    return reply(res, 429, { error: { code: 'email_cooldown' } }, { 'Retry-After': '37' });
  });
  const form = { method: 'POST', data: { old_password: 'campus1a' } };
  assert.equal((await request(path, form)).status, 401);
  assert.equal(requests.length, 0);
  const { cookie, context } = await login(request, 'alice@example.com');
  const response = await request(path, { ...form, cookie, headers: { 'X-Website-Context': context } });
  assert.equal(response.status, 429);
  assert.equal(response.headers.get('retry-after'), '37');
});

test('an old page cannot act on a different account after the browser cookie changes', async t => {
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') {
      const name = JSON.parse(input.body).email.split('@')[0];
      return reply(res, 200, pair(`access_${name}`, `refresh_${name}`));
    }
    return reply(res, 200, { id: input.headers.authorization });
  });
  const first = await login(request, 'alice@example.com');
  const second = await login(request, 'bob@example.com', first.cookie);
  const instancePath = '/v1/instances/3c82d230-f963-48db-87bb-9c8c7d66b977';
  const data = { name: '新名称', status: 'paused' };
  const edit = options => request(instancePath, { method: 'PATCH', data, ...options });
  const count = requests.length;
  const stale = await edit({ cookie: second.cookie, headers: { 'X-Website-Context': first.context } });
  assert.equal(stale.status, 401);
  assert.equal((await stale.json()).error.code, 'website_context_changed');
  assert.equal((await request('/v1/me', { cookie: first.cookie })).status, 401);
  assert.equal(requests.length, count);
  const current = { cookie: second.cookie, headers: { 'X-Website-Context': second.context } };
  assert.equal((await edit({ ...current, sameOrigin: false })).status, 403);
  assert.equal((await edit(current)).status, 200);
  const forwarded = requests.at(-1);
  assert.equal(forwarded.method, 'PATCH');
  assert.deepEqual(JSON.parse(forwarded.body), data);
  assert.equal(forwarded.headers.authorization, 'Bearer access_bob');
});

test('instance, conversation and turn management require the current same-origin session', async t => {
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.method === 'DELETE') return res.writeHead(204).end();
    if (input.path.endsWith('/withdrawal') && input.method === 'POST') return res.writeHead(204).end();
    return reply(res, 200, { id: 'alice' });
  });
  const instance = '/v1/instances/3c82d230-f963-48db-87bb-9c8c7d66b977';
  const conversation = '/v1/conversations/bf29cde2-3da6-4e9b-8a89-32a6f33f0470';
  const run = '/v1/runs/8b04b52d-e3b3-4108-b48b-670a876a52ac';
  const { cookie, context } = await login(request, 'alice@example.com');
  const options = { cookie, headers: { 'X-Website-Context': context } };
  const count = requests.length;
  assert.equal((await request(conversation, { ...options, method: 'DELETE', sameOrigin: false })).status, 403);
  assert.equal((await request(instance, { cookie, method: 'DELETE' })).status, 401);
  assert.equal((await request(run + '/withdrawal', { cookie })).status, 401);
  assert.equal((await request(run + '/withdrawal', { ...options, method: 'POST', sameOrigin: false })).status, 403);
  assert.equal(requests.length, count);

  assert.equal((await request(conversation, { ...options, method: 'PATCH', data: { title: '新标题' } })).status, 200);
  assert.deepEqual(JSON.parse(requests.at(-1).body), { title: '新标题' });
  assert.equal((await request(instance, { ...options, method: 'PATCH', data: { is_default: true } })).status, 200);
  assert.deepEqual(JSON.parse(requests.at(-1).body), { is_default: true });
  const retry = await request(run + '/retry', { ...options, method: 'POST', data: { attempt: 1 },
    headers: { ...options.headers, 'Idempotency-Key': 'retry-original-turn' } });
  assert.equal(retry.status, 200);
  assert.equal(requests.at(-1).headers['idempotency-key'], 'retry-original-turn');
  assert.equal((await request(run + '/withdrawal', options)).status, 200);
  const confirmation = { attempt: 1, approvals: ['56ff79a1-7ac9-4d40-b903-f18ba1d77e83'] };
  assert.equal((await request(run + '/withdrawal', { ...options, method: 'POST', data: confirmation })).status, 204);
  assert.deepEqual(JSON.parse(requests.at(-1).body), confirmation);
  for (const path of [conversation, instance]) {
    const deleted = await request(path, { ...options, method: 'DELETE' });
    assert.equal(deleted.status, 204);
    assert.equal(await deleted.text(), '');
    assert.equal(requests.at(-1).path, path);
    assert.equal(requests.at(-1).method, 'DELETE');
    assert.equal(requests.at(-1).headers.authorization, 'Bearer access_first');
  }
});

test('campus panels forward only the authenticated read routes and serve the local map', async t => {
  const campus = '/v1/campus-view';
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    if (input.path === campus) return reply(res, 200, { campus_id: 'cuc-beijing', points: [] });
    return reply(res, 200, { source: 'minta_simulation', items: [] });
  });
  const schedule = '/v1/minta/schedule?week=4';
  const activities = '/v1/minta/activities';
  const classrooms = '/v1/minta/classrooms';
  const report = '/v1/minta/semester-report';
  const html = await (await request('/')).text();
  assert.match(html, /data-campus-panel="schedule"[\s\S]*?个人日程/);
  assert.match(html, /data-campus-panel="semester-report"[\s\S]*?学期报告/);
  assert.match(html, /id="open-personal-space"[^>]*>个人空间/);
  const scheduleModule = await request('/campus-schedule.js');
  assert.equal(scheduleModule.status, 200);
  assert.match(scheduleModule.headers.get('content-type'), /text\/javascript/);
  assert.equal((await request('/campus-semester.js')).status, 200);
  const base = await request('/campus-map.geojson');
  assert.equal(base.status, 404);
  assert.equal((await base.json()).error.code, 'asset_unavailable');
  assert.equal((await request(schedule)).status, 401);
  assert.equal((await request(activities)).status, 401);
  assert.equal((await request(classrooms)).status, 401);
  assert.equal((await request(report)).status, 401);
  assert.equal((await request(campus)).status, 401);
  const { cookie, context } = await login(request, 'alice@example.com');
  const options = { cookie, headers: { 'X-Website-Context': context } };
  const before = requests.length;
  assert.equal((await request(schedule, { cookie })).status, 401);
  assert.equal((await request(activities, { ...options, method: 'POST', data: {} })).status, 404);
  assert.equal((await request('/v1/minta/call', { ...options, method: 'POST', data: {} })).status, 404);
  assert.equal(requests.length, before);

  for (const path of [schedule, activities, classrooms, campus, report]) {
    const response = await request(path, options);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const data = await response.json();
    if (path === campus) assert.equal(data.campus_id, 'cuc-beijing');
    else assert.equal(data.source, 'minta_simulation');
    const upstream = requests.at(-1);
    assert.equal(upstream.path, path);
    assert.equal(upstream.headers.authorization, 'Bearer access_first');
    assert.equal(upstream.headers.cookie, undefined);
    assert.equal(upstream.headers['x-website-context'], undefined);
  }
});

test('calendar links use a scoped credential and preserve conditional requests and downloads', async t => {
  const subscriptions = '/v1/calendar/subscriptions';
  const exportPath = '/v1/calendar/export.ics';
  const feedPath = '/v1/calendar/feed.ics';
  const secret = 'cal_' + 'a'.repeat(43);
  const link = '/calendar/' + secret + '.ics';
  const etag = 'W/"' + 'a'.repeat(64) + '"';
  const ics = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n';
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    if (input.path === subscriptions) {
      assert.equal(input.headers.authorization, 'Bearer access_first');
      assert.equal(input.method, 'POST');
      return reply(res, 201, { path: link });
    }
    assert.ok([feedPath, exportPath].includes(input.path));
    assert.equal(input.headers.authorization, 'Bearer ' + (input.path === feedPath ? secret : 'access_first'));
    assert.equal(input.headers.cookie, undefined);
    assert.equal(input.headers['x-website-context'], undefined);
    if (input.headers['if-none-match'] === etag) {
      res.writeHead(304, { ETag: etag }).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/calendar; charset=utf-8', 'Content-Length': Buffer.byteLength(ics), ETag: etag,
      'Content-Disposition': 'attachment; filename="unexpected.ics"' });
    res.end(input.method === 'HEAD' ? undefined : ics);
  });
  assert.match(await (await request('/')).text(), /data-campus-panel="calendar-sync"[\s\S]*?日历同步/);
  assert.equal((await request('/campus-calendar.js')).status, 200);
  assert.equal((await request(feedPath)).status, 404);
  assert.equal((await request(link + '?user=other')).status, 404);
  assert.equal((await request(exportPath)).status, 401);
  assert.equal(requests.length, 0);

  const response = await request(link, { headers: { Authorization: 'Bearer copied-session' } });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.match(response.headers.get('x-robots-tag'), /noindex/);
  assert.equal(response.headers.get('content-disposition'), null);
  assert.equal(response.headers.get('etag'), etag);
  assert.equal(response.headers.get('content-type'), 'text/calendar; charset=utf-8');
  assert.equal(await response.text(), ics);
  assert.equal((await request(link, { headers: { 'If-None-Match': etag } })).status, 304);
  const head = await request(link, { method: 'HEAD', sameOrigin: false });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get('content-length'), String(Buffer.byteLength(ics)));
  assert.equal(await head.text(), '');

  const { cookie, context } = await login(request, 'alice@example.com');
  const authenticated = { cookie, headers: { 'X-Website-Context': context } };
  const created = await request(subscriptions, { ...authenticated, method: 'POST', data: { label: '安卓' } });
  assert.equal(created.status, 201);
  assert.equal((await created.json()).path, link);
  assert.equal((await request(exportPath, { cookie })).status, 401);
  const download = await request(exportPath, authenticated);
  assert.equal(download.status, 200);
  assert.equal(download.headers.get('content-disposition'), 'attachment; filename="campus-schedule.ics"');
  assert.equal(await download.text(), ics);
});

test('personal calendar changes require the current session and retain the delete revision', async t => {
  const events = '/v1/calendar/events';
  const item = `${events}/3831fd05-9213-4c41-a687-4fdc8817ca3b?revision=3`;
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    assert.equal(input.path, item);
    assert.equal(input.method, 'DELETE');
    assert.equal(input.headers.authorization, 'Bearer access_first');
    return reply(res, 200, { deleted: true });
  });
  assert.equal((await request(`${events}?start=2026-11-09&end=2026-11-16`)).status, 401);
  const { cookie, context } = await login(request, 'alice@example.com');
  const options = { cookie, headers: { 'X-Website-Context': context }, method: 'DELETE' };
  const before = requests.length;
  assert.equal((await request(item, { ...options, sameOrigin: false })).status, 403);
  assert.equal(requests.length, before);
  const response = await request(item, options);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { deleted: true });
});

test('Microsoft To Do authorization is bound to the configured website and current session', async t => {
  const todo = '/v1/calendar/microsoft-todo';
  const callback = '/oauth/microsoft-todo/callback';
  const state = 's'.repeat(43);
  const code = 'fixture-oauth-code';
  let redirectHost;
  const { request, requests, origin } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    assert.equal(input.headers.authorization, 'Bearer access_first');
    assert.equal(input.headers.cookie, undefined);
    if (input.path === todo + '/start') {
      const destination = new URL('https://login.microsoftonline.com/common/oauth2/v2.0/authorize');
      destination.searchParams.set('redirect_uri', redirectHost + callback);
      return reply(res, 200, { authorization_url: destination.href });
    }
    assert.equal(input.path, todo + '/finish');
    assert.deepEqual(JSON.parse(input.body), { state, code });
    res.writeHead(204).end();
  });
  redirectHost = origin;
  assert.equal((await request(todo + '/start', { method: 'POST' })).status, 401);
  const rejected = await request(`${callback}?state=${state}&code=${code}`);
  assert.equal(rejected.headers.get('location'), '/?todo=failed');
  assert.equal(requests.length, 0);
  const { cookie, context } = await login(request, 'alice@example.com');
  const session = { cookie, headers: { 'X-Website-Context': context } };
  const started = await request(todo + '/start', { ...session, method: 'POST' });
  assert.equal(started.status, 200);
  assert.equal(new URL((await started.json()).authorization_url).searchParams.get('redirect_uri'), origin + callback);
  redirectHost = 'https://other-campus.test';
  assert.equal((await request(todo + '/start', { ...session, method: 'POST' })).status, 409);
  assert.equal((await request(todo + '/finish', { ...session, method: 'POST', data: { state, code } })).status, 404);
  const done = await request(`${callback}?state=${state}&code=${code}`, { cookie });
  assert.equal(done.status, 303);
  assert.equal(done.headers.get('location'), '/?todo=connected');
});

test('the WeChat feed uses the current login and forwards version events without buffering', async t => {
  const path = '/v1/campus-digest/wechat';
  const listeners = [];
  let expired = false;
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    if (expired && [path + '/events', '/v1/auth/refresh'].includes(input.path)) {
      return reply(res, 401, { error: { code: 'invalid_session' } });
    }
    assert.equal(input.headers.authorization, 'Bearer access_first');
    assert.equal(input.headers['x-website-context'], undefined);
    if (input.path === path + '/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
      res.write('data: 8\n\n');
      listeners.push(res);
      return;
    }
    assert.deepEqual(Object.fromEntries(new URL(input.path, 'http://fixture').searchParams), {
      account: '学生会', title: '志愿活动', offset: '12',
    });
    return reply(res, 200, { items: [{ title: '志愿活动招募', published_at: '2026-11-11T08:00:00+08:00' }],
      revision: { version: 8 }, page_size: 12, refresh_seconds: 5 });
  });
  const { cookie, context } = await login(request, 'alice@example.com');
  assert.equal((await request('/campus-digest.js')).status, 200);
  assert.equal((await request(path + '/events', { cookie })).status, 401);
  const opts = { cookie, headers: { 'X-Website-Context': context } };
  assert.equal((await request(path + '/events?upstream=other', opts)).status, 422);
  const feed = await request(path + '?' + new URLSearchParams({ account: '学生会', title: '志愿活动', offset: 12 }), opts);
  assert.equal(feed.status, 200);
  const data = await feed.json();
  assert.equal(data.items[0].published_at, '2026-11-11T08:00:00+08:00');
  assert.equal(data.refresh_seconds, 5);
  const response = await request(path + '/events', opts);
  assert.equal(response.headers.get('content-type'), 'text/event-stream; charset=utf-8');
  assert.equal(response.headers.get('x-accel-buffering'), 'no');
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  assert.deepEqual(await reader.read(), { value: 'data: 8\n\n', done: false });
  const other = (await request(path + '/events', opts)).body.pipeThrough(new TextDecoderStream()).getReader();
  assert.deepEqual(await other.read(), { value: 'data: 8\n\n', done: false });
  for (const listener of listeners) listener.end('data: 9\n\n');
  for (const tab of [reader, other]) {
    assert.deepEqual(await tab.read(), { value: 'data: 9\n\n', done: false });
    assert.equal((await tab.read()).done, true);
  }
  assert.equal(requests.filter(item => item.path === path + '/events').length, 2);
  expired = true;
  const denied = await request(path + '/events', opts);
  assert.equal(denied.status, 401);
  assert.equal((await denied.json()).error.code, 'website_session_expired');
});

test('campus photo uploads keep the site context and image tags only receive the approved photo route', async t => {
  const place = '/v1/campus-view/photo-places/1165519_399134/photos';
  const asset = '/v1/campus-view/photos/00000000-0000-4000-8000-000000000001/image';
  const bytes = Buffer.from([255, 216, 255, 217]);
  const largePhoto = Buffer.alloc(20 * 1024 * 1024, 0x7f);
  let published = false;
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    assert.equal(input.headers.authorization, 'Bearer access_first');
    if (input.path === place && input.method === 'POST') {
      assert.ok(input.raw.equals(largePhoto));
      assert.equal(input.headers['idempotency-key'], 'photo-key');
      return reply(res, 201, { status: 'pending' });
    }
    if (input.path === asset && published) {
      res.writeHead(200, { 'Content-Type': 'image/jpeg' });
      return res.end(bytes);
    }
    return reply(res, 404, { error: { message: 'Not published' } });
  });
  const { cookie, context } = await login(request, 'alice@example.com');
  assert.equal((await request(asset)).status, 401);
  const before = requests.length;
  assert.equal((await request(place, { method: 'POST', cookie, upload: bytes })).status, 401);
  assert.equal(requests.length, before);
  const headers = { 'X-Website-Context': context, 'Idempotency-Key': 'photo-key' };
  const submitted = await request(place, { method: 'POST', cookie, upload: largePhoto, headers: { ...headers } });
  assert.equal(submitted.status, 201);
  const oversized = await request(place, { method: 'POST', cookie, upload: Buffer.alloc(largePhoto.length + 1), headers });
  assert.equal(oversized.status, 413);
  assert.equal(requests.filter(input => input.path === place).length, 1);
  assert.equal((await request(asset, { cookie })).status, 404);
  published = true;
  const image = await request(asset, { cookie });
  assert.equal(image.headers.get('content-type'), 'image/jpeg');
  assert.deepEqual(Buffer.from(await image.arrayBuffer()), bytes);
});

test('an onboarding session can save its own campus profile through the website', async t => {
  const data = { full_name: '登记姓名', campus_number: '001234', college: '新闻学院', grade: 'ug1' };
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_profile', 'refresh_profile'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice', campus_status: 'profile_required' });
    assert.equal(input.method, 'PUT');
    assert.equal(input.path, '/v1/profile');
    assert.equal(input.headers.authorization, 'Bearer access_profile');
    assert.deepEqual(JSON.parse(input.body), data);
    return reply(res, 200, { id: 'alice', campus_status: 'ready', full_name: data.full_name });
  });
  const { cookie, context } = await login(request, 'alice@example.com');
  const denied = await request('/v1/profile', { method: 'PUT', data, cookie });
  assert.equal(denied.status, 401);
  const saved = await request('/v1/profile', { method: 'PUT', data, cookie, headers: { 'X-Website-Context': context } });
  assert.equal(saved.status, 200);
  assert.equal((await saved.json()).full_name, data.full_name);
  assert.equal(requests.filter(input => input.path === '/v1/profile').length, 1);
});

test('campus operations require the current website session and relay the idempotency key', async t => {
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path === '/v1/auth/login') return reply(res, 200, pair('access_first', 'refresh_first'));
    if (input.path === '/v1/me') return reply(res, 200, { id: 'alice' });
    return reply(res, 200, { state: 'active' });
  });
  const path = '/v1/minta/operations';
  const data = { service: 'library', resource_id: 'book-one' };
  const options = { method: 'POST', data, headers: { 'Idempotency-Key': 'example-key' } };
  assert.equal((await request(path, options)).status, 401);
  const { cookie, context } = await login(request, 'alice@example.com');
  const authorized = { ...options, cookie, headers: { ...options.headers, 'X-Website-Context': context } };
  const response = await request(path, authorized);
  assert.equal(response.status, 200);
  assert.equal(requests.at(-1).headers['idempotency-key'], 'example-key');
  assert.equal(requests.at(-1).headers.authorization, 'Bearer access_first');
  const query = '/v1/minta/catalog/library?q=Python&category=computing&page=2';
  assert.equal((await request(query)).status, 401);
  assert.equal((await request(query, { cookie, headers: { 'X-Website-Context': context } })).status, 200);
  assert.equal(requests.at(-1).path, query);
  assert.equal((await request('/v1/minta/library/api/loans', authorized)).status, 404);
  assert.equal((await request(path, { ...authorized, sameOrigin: false,
    headers: { 'X-Website-Context': context, Origin: 'https://outside.example.test' } })).status, 403);
});

test('handles the browser-bound SSO callback and exchanges the completion code server-side', async t => {
  let challenge;
  const { request, requests } = await fixture(t, (input, res) => {
    if (input.path.endsWith('/start')) {
      ({ code_challenge: challenge } = JSON.parse(input.body));
      return reply(res, 200, { authorization_url: 'https://school.example.com/login' }, {
        'Set-Cookie': ['cloud-sso-dev=browser_binding; Path=/; HttpOnly; SameSite=Lax', 'internal=not_forwarded'],
      });
    }
    if (input.path.includes('/callback?')) {
      assert.equal(input.path, '/v1/auth/sso/campus/callback?state=school_state&code=school_code');
      assert.equal(input.headers.cookie, 'cloud-sso-dev=browser_binding');
      return reply(res, 200, { completion_code: 'short_lived', mode: 'login' });
    }
    const data = JSON.parse(input.body);
    assert.equal(data.completion_code, 'short_lived');
    assert.equal(createHash('sha256').update(data.code_verifier).digest('base64url'), challenge);
    return reply(res, 200, pair('access_sso', 'refresh_sso'));
  });
  const callback = '/v1/auth/sso/campus/callback?state=school_state&code=school_code';
  const missing = await request(callback);
  assert.equal(missing.headers.get('location'), '/?campus=failed');
  assert.equal(requests.length, 0);
  const start = await request('/v1/auth/sso/campus/start', { method: 'POST', data: { mode: 'login' } });
  assert.deepEqual(await start.json(), { authorization_url: 'https://school.example.com/login' });
  const binding = cookiesFrom(start);
  assert.equal(binding.length, 2);
  const complete = await request(callback, { cookie: [...binding, 'unrelated=browser_data'].join('; ') });
  assert.equal(complete.status, 303);
  assert.equal(complete.headers.get('location'), '/?campus=signed-in');
  assert.doesNotMatch(complete.headers.get('set-cookie'), /access_sso|refresh_sso|short_lived/);
  assert.match(complete.headers.get('set-cookie'), /campus-desk-dev=[\w-]+/);
  assert.equal(await complete.text(), '');
  assert.equal(requests.length, 3);
});

test('proxies only the local CAS login pages and keeps validation private', async t => {
  const requestsToCas = [];
  const callback = '/v1/auth/sso/campus/callback?state=local_state&ticket=ST-test';
  const casUrl = await bind(createServer(async (req, res) => {
    requestsToCas.push([req.method, req.url, req.headers]);
    for await (const _ of req) { /* consume submitted form */ }
    if (req.method === 'POST') {
      res.writeHead(303, { Location: callback, 'Set-Cookie': [
        'campus-cas-session=TGT-test; Path=/; HttpOnly; SameSite=Lax', 'internal=not_forwarded',
      ] });
      res.end();
    } else {
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Referrer-Policy': 'strict-origin',
        'Set-Cookie': 'campus-cas-flow=LT-test; Path=/; HttpOnly',
      });
      res.end('<form method="post"></form>');
    }
  }), t);
  let local;
  let destination;
  const { origin, request } = await fixture(t, (input, res) => {
    assert.equal(input.path, '/v1/auth/sso/campus/start');
    return reply(res, 200, { authorization_url: destination, local_cas: local }, {
      'Set-Cookie': 'cloud-sso-dev=browser_binding; Path=/; HttpOnly; SameSite=Lax',
    });
  }, { casUrl });

  const start = () => request('/v1/auth/sso/campus/start', { method: 'POST', data: { mode: 'login' } });
  local = true;
  destination = origin + '/cas/login?service=test';
  assert.deepEqual(await (await start()).json(), { authorization_url: destination });
  destination = 'http://127.0.0.1:2/cas/login?service=test';
  assert.equal((await start()).status, 409);
  local = false;
  destination = origin + '/cas/login';
  assert.equal((await start()).status, 502);

  const page = await request('/cas/login?service=test', { cookie: 'cloud-sso-dev=private; campus-cas-session=TGT-test' });
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('referrer-policy'), 'strict-origin');
  assert.match(await page.text(), /<form/);
  assert.equal(requestsToCas[0][2].cookie, 'campus-cas-session=TGT-test');
  assert.equal(requestsToCas[0][2]['x-cas-site-origin'], origin);
  assert.equal(requestsToCas[0][2]['x-cas-client-ip'], '127.0.0.1');
  assert.equal((await request('/cas/p3/serviceValidate?ticket=ST-test')).status, 404);
  assert.equal((await request('/cas/login', { method: 'POST', sameOrigin: false })).status, 403);

  const submit = await fetch(origin + '/cas/login', {
    method: 'POST', redirect: 'manual',
    headers: { origin, cookie: cookiesFrom(page)[0], 'content-type': 'application/x-www-form-urlencoded' },
    body: 'lt=LT-test&username=test&password=local-test1',
  });
  assert.equal(submit.status, 303);
  assert.equal(submit.headers.get('location'), callback);
  assert.deepEqual(cookiesFrom(submit), ['campus-cas-session=TGT-test']);
  assert.equal(requestsToCas.length, 2);
  assert.equal(requestsToCas[1][2].cookie, 'campus-cas-flow=LT-test');
});

test('admin page reauthenticates the email account and binds its management session', async t => {
  const bridge = 'b'.repeat(43);
  const csrf = 'c'.repeat(64);
  const session = 'oa_' + 's'.repeat(43);
  const email = 'operator@example.com';
  let siteBinding;
  let access = 'access_admin';
  let expired = false;
  let revoked = false;
  const { origin, request, requests } = await fixture(t, (incoming, res) => {
    if (incoming.path === '/v1/auth/login') {
      assert.deepEqual(JSON.parse(incoming.body), { email, password: 'test' });
      assert.equal(incoming.headers.authorization, undefined);
      assert.equal(incoming.headers['x-campus-admin-bridge'], undefined);
      return reply(res, 200, pair(access, 'refresh_admin'));
    }
    if (incoming.path === '/v1/auth/refresh') {
      assert.equal(expired, true);
      assert.deepEqual(JSON.parse(incoming.body), { refresh_token: 'refresh_admin' });
      expired = false;
      access = 'access_refreshed';
      return reply(res, 200, pair(access, 'refresh_rotated'));
    }
    assert.equal(incoming.headers.authorization, `Bearer ${access}`);
    if (incoming.path === '/v1/me') return reply(res, 200, { email });
    assert.equal(incoming.headers['x-campus-admin-bridge'], bridge);
    assert.equal(incoming.headers['x-campus-admin-origin'], origin);
    if (incoming.path === '/admin/api/login') {
      assert.equal(incoming.headers.origin, origin);
      assert.equal(incoming.headers.cookie, undefined);
      assert.equal(incoming.body, '');
      siteBinding = incoming.headers['x-campus-admin-site-session'];
      assert.match(siteBinding, /^[a-f0-9]{64}$/);
      return reply(res, 200, { name: email, csrf }, { 'Set-Cookie': [
        `campus-admin-dev=${session}; Path=/; HttpOnly; SameSite=Strict`, 'ordinary=never_forwarded',
      ] });
    }
    assert.equal(incoming.headers['x-campus-admin-site-session'], siteBinding);
    assert.equal(incoming.headers.cookie, `campus-admin-dev=${session}`);
    if (incoming.path.startsWith('/admin/rumor/')) {
      if (revoked) return reply(res, 401, { error: { code: 'admin_login_required' } });
      if (incoming.path.endsWith('/admin')) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': "default-src 'none'",
          'Set-Cookie': 'rumor=private' });
        return res.end('<h1>Rumor review</h1>');
      }
      assert.equal(incoming.headers.origin, origin);
      assert.equal(incoming.headers['x-campus-request'], '1');
      assert.deepEqual(JSON.parse(incoming.body), { status: 'reviewed' });
      return reply(res, 200, { ok: true });
    }
    assert.equal(incoming.headers['x-campus-admin-csrf'], csrf);
    if (revoked) return reply(res, 401, { error: { code: 'admin_login_required' } });
    if (expired) return reply(res, 401, { error: { code: 'admin_site_session_invalid' } });
    reply(res, 200, { items: [] });
  }, { env: { MPM_WEB_ADMIN_BRIDGE: bridge } });
  const page = await request('/admin');
  assert.equal(page.status, 200);
  assert.match(await page.text(), /name="email" type="email"/);
  assert.equal((await request('/admin/panel.js')).status, 200);
  assert.equal((await request('/admin/api/login', { method: 'POST', data: {}, sameOrigin: false })).status, 403);
  const signed = await request('/admin/api/login', { method: 'POST', data: { email, password: 'test', remember_me: true },
    cookie: 'campus-desk-dev=ordinary', headers: { 'x-campus-admin-bridge': 'forged', 'x-campus-admin-site-session': 'forged' } });
  assert.equal(signed.status, 200);
  assert.deepEqual(await signed.json(), { name: email, csrf });
  const [adminCookie, siteCookie, ...extraCookies] = cookiesFrom(signed);
  assert.equal(adminCookie, `campus-admin-dev=${session}`);
  assert.match(siteCookie, /^campus-desk-dev=[A-Za-z0-9_-]{43}$/);
  assert.deepEqual(extraCookies, []);
  assert.equal((await request('/admin/api/session', { cookie: siteCookie })).status, 401);
  const cookie = `${adminCookie}; ${siteCookie}; cloud-sso-dev=sso`;
  assert.equal((await request('/admin/rumor')).headers.get('location'), '/admin/rumor/admin');
  assert.equal((await request('/admin/rumor/admin', { cookie: siteCookie })).headers.get('location'), '/admin');
  const rumorAdmin = await request('/admin/rumor/admin', { cookie });
  assert.equal(rumorAdmin.status, 200);
  assert.match(await rumorAdmin.text(), /Rumor review/);
  assert.equal(rumorAdmin.headers.get('content-security-policy'), "default-src 'none'");
  assert.equal(rumorAdmin.headers.get('set-cookie'), null);
  const tipPath = '/admin/rumor/api/tips/1/status';
  const review = { cookie, method: 'POST', data: { status: 'reviewed' } };
  assert.equal((await request(tipPath, review)).status, 403);
  assert.equal((await request(tipPath, { ...review, headers: { 'X-Campus-Request': '1' } })).status, 200);
  assert.equal((await request('/admin/api/login?from=other', { method: 'POST', cookie })).status, 422);
  const load = () => request('/admin/api/data/users', {
    cookie,
    headers: { 'x-campus-admin-csrf': csrf, authorization: 'Bearer forged' },
  });
  assert.deepEqual(await (await load()).json(), { items: [] });
  const photo = '/admin/api/campus/photos/review/00000000-0000-4000-8000-000000000001?version=2';
  const photoOptions = { cookie, method: 'DELETE', headers: { 'x-campus-admin-csrf': csrf } };
  assert.equal((await request(photo, { ...photoOptions, sameOrigin: false })).status, 403);
  assert.equal((await request(photo, photoOptions)).status, 200);
  assert.equal(requests.at(-1).method, 'DELETE');
  assert.equal(requests.at(-1).path, photo);
  expired = true;
  assert.equal((await load()).status, 200);
  revoked = true;
  assert.equal((await load()).status, 401);
  assert.equal((await request('/admin/rumor/admin', { cookie })).headers.get('location'), '/admin');
  assert.equal((await request('/v1/me', { cookie })).status, 200);
  assert.equal((await request('/admin/minta/v1/call', { method: 'POST', data: {} })).status, 404);
  assert.equal(requests.filter(({ path }) => path === '/v1/auth/refresh').length, 1);
  assert.equal(requests.filter(({ path }) => path === '/admin/api/login').length, 1);
});

test('a denied admin login discards its temporary auth session and preserves the current user session', async t => {
  const { request, requests } = await fixture(t, (incoming, res) => {
    if (incoming.path === '/v1/auth/login') {
      const access = JSON.parse(incoming.body).email === 'current@example.com' ? 'access_current' : 'access_denied';
      return reply(res, 200, pair(access, 'refresh'));
    }
    if (incoming.path === '/admin/api/login') return reply(res, 403, { error: { code: 'admin_permission_required' } });
    if (incoming.path === '/v1/auth/logout') {
      assert.equal(incoming.headers.authorization, 'Bearer access_denied');
      return res.writeHead(204).end();
    }
    assert.equal(incoming.headers.authorization, 'Bearer access_current');
    return reply(res, 200, { id: 'current' });
  }, { env: { MPM_WEB_ADMIN_BRIDGE: 'b'.repeat(43) } });
  const { cookie } = await login(request, 'current@example.com');
  const denied = await request('/admin/api/login', { method: 'POST', cookie,
    data: { email: 'other@example.com', password: 'test' } });
  assert.equal(denied.status, 403);
  assert.deepEqual(cookiesFrom(denied), []);
  assert.equal((await request('/v1/me', { cookie })).status, 200);
  assert.equal(requests.filter(({ path }) => path === '/v1/auth/logout').length, 1);
});

test('redirects to the separate CAS origin and finishes the browser flow on the Agent origin', async t => {
  const agent = 'https://agent.example.com';
  const cas = 'https://cas.example.com';
  const startPath = '/v1/auth/sso/campus/start';
  const callback = '/v1/auth/sso/campus/callback?state=example&ticket=ST-example';
  let destination = `${cas}/cas/login?service=${encodeURIComponent(agent + callback)}`;
  const { request, requests } = await fixture(t, (incoming, res) => {
    if (incoming.path === startPath) {
      assert.equal(JSON.parse(incoming.body).website_origin, agent);
      return reply(res, 200, { authorization_url: destination, local_cas: true }, {
        'Set-Cookie': '__Host-cloud-sso=browser_binding; Path=/; Secure; HttpOnly; SameSite=Lax',
      });
    }
    if (incoming.path === callback) {
      assert.equal(incoming.headers.cookie, '__Host-cloud-sso=browser_binding');
      return reply(res, 200, { completion_code: 'one-time-code', mode: 'login' });
    }
    assert.equal(incoming.path, '/v1/auth/sso/campus/finish');
    return reply(res, 200, pair('access_cas', 'refresh_cas'));
  }, { env: { USER_WEB_PUBLIC_URL: agent, USER_WEB_CAS_PUBLIC_URL: cas } });
  const start = () => request(startPath, {
    method: 'POST', data: { mode: 'login', website_origin: 'https://unregistered.example.com' },
  });
  const opened = await start();
  assert.equal(opened.status, 200);
  assert.deepEqual(await opened.json(), { authorization_url: destination });
  assert.ok(opened.headers.getSetCookie().every(value => value.includes('Secure')));
  const finished = await request(callback, { cookie: cookiesFrom(opened).join('; ') });
  assert.equal(finished.status, 303);
  assert.equal(finished.headers.get('location'), '/?campus=signed-in');
  assert.equal(requests.length, 3);
  assert.equal((await request('/cas/login')).status, 404);
  destination = agent + '/cas/login?service=test';
  assert.equal((await start()).status, 409);
});
