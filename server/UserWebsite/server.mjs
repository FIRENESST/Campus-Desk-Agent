import { createHash, randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { isIP } from 'node:net';
import { resolve } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { pathToFileURL } from 'node:url';

import { APK_FILENAME, APK_PATH, APK_TYPE, DOWNLOAD_PORT, MAX_RELEASE_BYTES, QR_PATH, qrOrigin } from '../AndroidAppServer/server.mjs';

const client = new URL('../../client/UIF/', import.meta.url);
const adminClient = new URL('../../client/Admin/', import.meta.url);
const adminIndex = new URL('index.html', adminClient);
const navigation = process.env.CAMPUS_NAVIGATION_DIR?.trim();
const loginAssets = new URL('../CASserver/web/', import.meta.url);
const avatarModel = 'seethrough_output_1';
const avatarAssets = [
  ...['pixi.min.js', 'unsafe-eval.min.js', 'live2dcubismcore.min.js', 'cubism4.min.js']
    .map(file => [`vendor/${file}`, 'text/javascript; charset=utf-8']),
  ...['model3', 'physics3', 'cdi3', 'idle.motion3', 'nod.motion3', 'shake.motion3', 'blink.motion3',
    'smile.exp3', 'sad.exp3', 'angry.exp3', 'surprised.exp3']
    .map(file => [`model/${avatarModel}.${file}.json`, 'application/json; charset=utf-8']),
  [`model/${avatarModel}.moc3`, 'application/octet-stream'],
  [`model/${avatarModel}.4096/texture_00.png`, 'image/png'],
];
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/privacy', ['privacy.html', 'text/html; charset=utf-8']],
  ['/privacy.css', ['privacy.css', 'text/css; charset=utf-8']],
  ['/auth/password.css', [new URL('password.css', loginAssets), 'text/css; charset=utf-8']],
  ['/auth/password.js', [new URL('password.js', loginAssets), 'text/javascript; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/campus-avatar.js', ['campus-avatar.js', 'text/javascript; charset=utf-8']],
  ['/chat-companion.js', ['chat-companion.js', 'text/javascript; charset=utf-8']],
  ['/companion/still.png', ['companion/still.png', 'image/png']],
  ...['arrive', 'reading', 'smile'].map(name => [
    `/companion/${name}.gif`, [`companion/${name}.gif`, 'image/gif'],
  ]),
  ...avatarAssets.map(([name, type]) => [`/live2d/${name}`, [`live2d/${name}`, type]]),
  ['/markdown.mjs', ['markdown.mjs', 'text/javascript; charset=utf-8']],
  ['/vendor/markdown-it-15.0.3.mjs', ['vendor/markdown-it-15.0.3.mjs', 'text/javascript; charset=utf-8']],
  ['/panels.js', ['panels.js', 'text/javascript; charset=utf-8']],
  ['/campus-schedule.js', ['campus-schedule.js', 'text/javascript; charset=utf-8']],
  ['/campus-calendar.js', ['campus-calendar.js', 'text/javascript; charset=utf-8']],
  ['/campus-semester.js', ['campus-semester.js', 'text/javascript; charset=utf-8']],
  ['/campus-digest.js', ['campus-digest.js', 'text/javascript; charset=utf-8']],
  ['/campus-rumor.js', ['campus-rumor.js', 'text/javascript; charset=utf-8']],
  ['/campus-operations.js', ['campus-operations.js', 'text/javascript; charset=utf-8']],
  ['/campus-map.js', ['campus-map.js', 'text/javascript; charset=utf-8']],
  ['/campus-map-3d.js', ['campus-map-3d.js', 'text/javascript; charset=utf-8']],
  ['/campus-map-scene.js', ['campus-map-scene.js', 'text/javascript; charset=utf-8']],
  ['/campus-photos.js', ['campus-photos.js', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/campus.svg', ['campus.svg', 'image/svg+xml']],
  ['/campus-rings.svg', ['campus-rings.svg', 'image/svg+xml']],
  ['/campus-atlas.svg', ['campus-atlas.svg', 'image/svg+xml']],
  ['/admin', [adminIndex, 'text/html; charset=utf-8']],
  ['/admin/', [adminIndex, 'text/html; charset=utf-8']],
  ['/admin/panel.js', [new URL('panel.js', adminClient), 'text/javascript; charset=utf-8']],
  ['/admin/panel.css', [new URL('panel.css', adminClient), 'text/css; charset=utf-8']],
  ['/campus-map.geojson', [
    navigation ? pathToFileURL(resolve(navigation, 'base_map.geojson'))
      : new URL('../../DataBase/CampusNavigation/local/base_map.geojson', import.meta.url),
    'application/geo+json; charset=utf-8',
  ]],
]);
const callbackPath = '/v1/auth/sso/campus/callback';
const startPath = '/v1/auth/sso/campus/start';
const metadataPath = '/v1/auth/sso/campus/metadata';
const cloudCookies = ['__Host-cloud-sso', 'cloud-sso-dev'];
const casCookies = ['campus-cas-flow', 'campus-cas-session', '__Host-campus-cas-flow', '__Host-campus-cas-session'];
const casPaths = new Map([
  ['/cas', ['GET']],
  ['/cas/login', ['GET', 'POST']],
  ['/cas/logout', ['GET', 'POST']],
  ['/cas/login.css', ['GET']],
  ['/cas/privacy', ['GET']],
  ['/cas/password.js', ['GET']],
  ['/cas/password.css', ['GET']],
]);
const formMedia = 'application/x-www-form-urlencoded';
const adminPath = /^\/admin\/api\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/;
const rumorAdminPath = '/admin/rumor/';
const rumorAdminMedia = new Set(['text/html', 'text/css', 'text/javascript', 'application/javascript',
  'application/json', 'image/png', 'image/jpeg', 'image/webp', 'application/octet-stream', 'text/plain', 'text/markdown']);
const ttl = 7 * 24 * 60 * 60;
const rememberTtl = 30 * 24 * 60 * 60;
const rememberedId = /^rm_[A-Za-z0-9_-]{43}$/;
const maxSessions = 4096;
const loopback = new Set(['127.0.0.1', 'localhost', '[::1]']);
const openPost = new Set([
  '/v1/auth/register', '/v1/auth/verification/request', '/v1/auth/verification/confirm',
  '/v1/auth/password/request', '/v1/auth/password/reset',
]);
const uuid = '[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}';
const photoGalleryPath = /^\/v1\/campus-view\/photo-places\/[0-9]{7}_[0-9]{6}\/photos$/;
const photoImagePath = new RegExp(`^/v1/campus-view/photos/${uuid}/image$`);
const adminPhotoImagePath = new RegExp(`^/admin/api/campus/photos/review/${uuid}/image$`);
const photoUploadLimit = 20 * 1024 * 1024;
const digestEventsPath = '/v1/campus-digest/wechat/events';
const calendarFeedPath = /^\/calendar\/(cal_[A-Za-z0-9_-]{43})\.ics$/;
const calendarExportPath = '/v1/calendar/export.ics';
const calendarEventPath = new RegExp(`^/v1/calendar/events/${uuid}$`);
const todoPath = '/v1/calendar/microsoft-todo';
const todoCallback = '/oauth/microsoft-todo/callback';
const userRoutes = [
  ['GET', /^\/v1\/(?:me|auth\/identities|instances)$/],
  ['GET|PUT', /^\/v1\/profile$/],
  ['GET|POST', /^\/v1\/calendar\/events$/],
  ['PUT|DELETE', calendarEventPath],
  ['GET|POST', /^\/v1\/calendar\/subscriptions$/],
  ['DELETE', new RegExp(`^/v1/calendar/subscriptions/${uuid}$`)],
  ['GET|HEAD', /^\/v1\/calendar\/export\.ics$/],
  ['GET|DELETE', /^\/v1\/calendar\/microsoft-todo$/],
  ['POST', /^\/v1\/calendar\/microsoft-todo\/sync$/],
  ['GET', /^\/v1\/campus-digest\/wechat(?:\/accounts)?$/],
  ['GET|PUT', /^\/v1\/campus-digest\/wechat\/following$/],
  ['GET', /^\/v1\/campus-rumor$/],
  ['POST', /^\/v1\/campus-rumor\/tips$/],
  ['GET', /^\/v1\/minta\/(?:schedule|activities|classrooms|semester-report)$/],
  ['GET', /^\/v1\/minta\/(?:catalog\/(?:classroom|gym|club|library)|teacher-credential)$/],
  ['GET|POST', /^\/v1\/minta\/(?:operations|clubs)$/],
  ['DELETE', new RegExp(`^/v1/minta/operations/${uuid}$`)],
  ['GET', /^\/v1\/campus-view(?:\/photo-places)?$/],
  ['POST', /^\/v1\/campus-view\/resolve$/],
  ['GET|POST', photoGalleryPath],
  ['GET', photoImagePath],
  ['POST', /^\/v1\/(?:instances|auth\/(?:logout|password\/change(?:\/request)?))$/],
  ['GET|PATCH|DELETE', new RegExp(`^/v1/instances/${uuid}$`)],
  ['GET|POST', new RegExp(`^/v1/instances/${uuid}/conversations$`)],
  ['PATCH|DELETE', new RegExp(`^/v1/conversations/${uuid}$`)],
  ['GET', new RegExp(`^/v1/(?:conversations/${uuid}/messages|runs/${uuid})$`)],
  ['GET|POST', new RegExp(`^/v1/runs/${uuid}/withdrawal$`)],
  ['POST', new RegExp(`^/v1/(?:conversations/${uuid}/runs|runs/${uuid}/(?:cancel|retry))$`)],
];

class SiteError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function readOrigin(value, variable) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      url.pathname !== '/' || url.search || url.hash ||
      (url.protocol === 'http:' && !loopback.has(url.hostname))) {
    throw new Error(`${variable} 需要 HTTPS 源地址，本地回环允许 HTTP`);
  }
  return url;
}

export function settings(env = process.env) {
  const host = env.USER_WEB_HOST || '127.0.0.1';
  const port = Number(env.USER_WEB_PORT || 8082);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('USER_WEB_PORT 无效');
  const publicUrl = readOrigin(env.USER_WEB_PUBLIC_URL || `http://127.0.0.1:${port}`, 'USER_WEB_PUBLIC_URL');
  const uimUrl = readOrigin(env.USER_WEB_UIM_URL || 'http://127.0.0.1:8090', 'USER_WEB_UIM_URL');
  const casPublicUrl = env.USER_WEB_CAS_PUBLIC_URL && readOrigin(env.USER_WEB_CAS_PUBLIC_URL, 'USER_WEB_CAS_PUBLIC_URL');
  const trustedProxy = env.USER_WEB_TRUSTED_PROXY || '';
  if (trustedProxy && !isIP(trustedProxy)) throw new Error('USER_WEB_TRUSTED_PROXY 必须为单个 IP');
  const secure = publicUrl.protocol === 'https:';
  if (casPublicUrl && (!secure || casPublicUrl.protocol !== 'https:' || casPublicUrl.origin === publicUrl.origin)) {
    throw new Error('CAS 和校园助手需要各自的 HTTPS 源地址');
  }
  if (!secure && !loopback.has(host) && host !== '::1') throw new Error('非本机监听需要 HTTPS 对外地址');
  return { host, port, publicUrl, uimUrl, casPublicUrl, trustedProxy, secure, adminBridge: env.MPM_WEB_ADMIN_BRIDGE || '' };
}

function cookieValue(request, name) {
  const value = request.headers.cookie?.split(';').map(part => part.trim())
    .find(part => part.startsWith(`${name}=`))?.slice(name.length + 1) || '';
  return /^[A-Za-z0-9_-]{1,256}$/.test(value) ? value : '';
}

function cookie(name, value, age, secure, sameSite = 'Lax', path = '/') {
  const lifetime = age === null ? '' : `; Max-Age=${age}`;
  return `${name}=${value}; Path=${path}; HttpOnly; SameSite=${sameSite}${lifetime}${secure ? '; Secure' : ''}`;
}

function answer(res, status, body = '', headers = {}) {
  res.writeHead(status, headers);
  res.end(body);
}

function json(res, status, body, headers = {}) {
  answer(res, status, JSON.stringify(body), { 'Content-Type': 'application/json; charset=utf-8', ...headers });
}

function gatewayError(status = 502) {
  return new SiteError(status, 'website_upstream', status === 504 ? '统一接口响应超时，请稍后重试。' : '暂时无法连接校园服务，请稍后重试。');
}

async function readBody(req, media = 'application/json', max = 65536) {
  if (req.headers['content-type']?.split(';')[0]?.trim().toLowerCase() !== media) {
    throw new SiteError(415, 'invalid_content_type', '请求内容类型不受支持。');
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of req.iterator({ destroyOnReturn: false })) {
    size += chunk.length;
    if (size > max) throw new SiteError(413, 'body_too_large', '请求内容过大。');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function replyJson(reply) {
  try {
    return JSON.parse(reply.body);
  } catch {
    throw gatewayError();
  }
}

function replyToBrowser(res, reply, extraMedia) {
  if (reply.status === 204) return answer(res, 204);
  const headers = {};
  const media = reply.headers.get('content-type')?.split(';')[0];
  if (media === 'application/json' || (extraMedia && media === extraMedia)) {
    headers['Content-Type'] = reply.headers.get('content-type');
  } else {
    throw gatewayError();
  }
  const retry = reply.headers.get('retry-after');
  if (retry) headers['Retry-After'] = retry;
  answer(res, reply.status, reply.body, headers);
}

function tokens(data, accessOnly = false) {
  const valid = value => typeof value === 'string' && /^[\w-]{1,256}$/.test(value);
  if (!valid(data?.access_token) || (!accessOnly && !valid(data.refresh_token))) throw gatewayError();
  return { access: data.access_token, refresh: accessOnly ? null : data.refresh_token };
}

function rememberAge(value) {
  if (!Number.isSafeInteger(value) || value < 1 || value > rememberTtl) throw gatewayError();
  return value;
}

export function createWebsite(config = settings(), { casUrl = 'http://127.0.0.1:8193', androidUrl = `http://127.0.0.1:${DOWNLOAD_PORT}` } = {}) {
  const { publicUrl, uimUrl, casPublicUrl, trustedProxy, secure, adminBridge } = config;
  const sessionName = secure ? '__Host-campus-desk' : 'campus-desk-dev';
  const flowName = secure ? '__Host-campus-desk-sso' : 'campus-desk-sso-dev';
  const flowSameSite = secure ? 'None' : 'Lax';
  const adminSessionName = secure ? '__Host-campus-admin' : 'campus-admin-dev';
  const sessions = new Map();
  const recovering = new Map();

  function clientAddress(req) {
    const peer = req.socket.remoteAddress?.replace(/^::ffff:/, '');
    const forwarded = req.headers['x-forwarded-for'];
    const address = trustedProxy && peer === trustedProxy && isIP(forwarded || '') ? forwarded : peer;
    return address && isIP(address) ? address : '';
  }

  async function upstream(req, path, {
    method = req.method, body, media, access, binding, adminSession, events, rumorAdmin, calendar = false,
  } = {}) {
    const accept = calendar ? 'text/calendar, application/json' : events ? 'text/event-stream, application/json' : 'application/json';
    const headers = new Headers({ accept,
      'x-forwarded-proto': publicUrl.protocol.slice(0, -1) });
    const etag = req.headers['if-none-match'];
    if (calendar && typeof etag === 'string' && etag.length <= 512) headers.set('if-none-match', etag);
    const address = clientAddress(req);
    if (address) headers.set('x-forwarded-for', address);
    if (req.headers['user-agent']) headers.set('user-agent', req.headers['user-agent'].slice(0, 200));
    if (access) headers.set('authorization', `Bearer ${access}`);
    if (binding) headers.set('cookie', binding);
    if (body !== undefined) headers.set('content-type', media || 'application/json');
    if (adminSession) {
      headers.set('x-campus-admin-bridge', adminBridge);
      headers.set('x-campus-admin-origin', publicUrl.origin);
      const siteBinding = createHash('sha256').update('campus-admin-site\0').update(adminSession).digest('hex');
      headers.set('x-campus-admin-site-session', siteBinding);
      if (address) headers.set('x-campus-admin-ip', address);
      if (method !== 'GET') headers.set('origin', publicUrl.origin);
      const csrf = req.headers['x-campus-admin-csrf'];
      if (typeof csrf === 'string' && /^[a-f0-9]{64}$/.test(csrf)) headers.set('x-campus-admin-csrf', csrf);
      if (rumorAdmin && req.headers['x-campus-request'] === '1') headers.set('x-campus-request', '1');
    }
    if ((photoGalleryPath.test(path) || /^\/v1\/(?:conversations\/[^/]+\/runs|runs\/[^/]+\/retry|minta\/(?:operations|clubs))$/.test(path))
        && req.headers['idempotency-key']) {
      headers.set('idempotency-key', req.headers['idempotency-key']);
    }
    const timeout = AbortSignal.timeout(events ? 105_000 : adminSession ? 190_000 : 25_000);
    const signal = events ? AbortSignal.any([events, timeout]) : timeout;
    try {
      const response = await fetch(new URL(path, uimUrl), { method, headers, body, signal, redirect: rumorAdmin ? 'manual' : 'error' });
      if (events && response.ok && response.headers.get('content-type')?.split(';')[0] === 'text/event-stream') {
        return { status: response.status, headers: response.headers, stream: response.body };
      }
      const chunks = [];
      let size = 0;
      if (response.body) {
        for await (const chunk of response.body) {
          size += chunk.byteLength;
          if (size > (rumorAdmin ? 18 : 8) * 1024 * 1024) throw gatewayError();
          chunks.push(chunk);
        }
      }
      return { status: response.status, headers: response.headers, body: Buffer.concat(chunks) };
    } catch {
      throw gatewayError(timeout.aborted ? 504 : 502);
    }
  }

  function getSession(id) {
    const session = sessions.get(id);
    if (!session || session.until <= Date.now()) {
      dropSession(id);
      throw new SiteError(401, 'website_login_required', '请先登录。');
    }
    return session;
  }

  function dropSession(id) {
    for (const stream of sessions.get(id)?.digestStreams || []) stream.abort();
    sessions.delete(id);
  }

  function checkContext(req, session) {
    if (req.headers['x-website-context'] !== session.context) {
      throw new SiteError(401, 'website_context_changed', '浏览器的登录会话已切换，请重新载入账号。');
    }
  }

  function checkCapacity(replacing) {
    for (const [id, session] of sessions) {
      if (session.until <= Date.now()) dropSession(id);
    }
    if (!sessions.has(replacing) && sessions.size >= maxSessions) {
      throw new SiteError(503, 'website_session_limit', '登录人数较多，请稍后重试。');
    }
  }

  function cacheSession(id, pair, age) {
    // Recreate the same page context after a restart while keeping it specific to this login.
    const context = rememberedId.test(id)
      ? createHash('sha256').update('website-context\0').update(id).digest('base64url')
      : randomBytes(18).toString('base64url');
    const session = { pair, until: Date.now() + age * 1000, refreshing: null, context };
    sessions.set(id, session);
    return session;
  }

  function signIn(previousId, reply, passwordLogin = false, newId) {
    const data = replyJson(reply);
    const pair = tokens(data);
    const remember = passwordLogin && data.remember_token !== undefined;
    if (remember && (typeof data.remember_token !== 'string' || !rememberedId.test(data.remember_token))) {
      throw gatewayError();
    }
    const age = remember ? rememberAge(data.remember_expires_in) : ttl;
    const id = remember ? data.remember_token : (newId ?? randomBytes(32).toString('base64url'));
    checkCapacity(previousId);
    dropSession(previousId);
    cacheSession(id, pair, age);
    return cookie(sessionName, id, passwordLogin && !remember ? null : age, secure);
  }

  async function rememberedAccess(req, id) {
    const reply = await upstream(req, '/v1/auth/remember', {
      method: 'POST', body: JSON.stringify({ token: id }),
    });
    if (reply.status === 401 || reply.status === 403) {
      throw new SiteError(401, 'website_session_expired', '登录已过期，请重新登录。');
    }
    if (reply.status === 429) throw new SiteError(429, 'website_rate_limited', '请求过于频繁，请稍后重试。');
    if (reply.status !== 200) throw gatewayError();
    const data = replyJson(reply);
    return { pair: tokens(data, true), age: rememberAge(data.remember_expires_in) };
  }

  async function ensureSession(req, id) {
    const session = sessions.get(id);
    if (session && session.until > Date.now()) return session;
    if (!rememberedId.test(id)) return getSession(id);
    if (recovering.has(id)) return recovering.get(id);
    checkCapacity(id);
    const pending = (async () => {
      const { pair, age } = await rememberedAccess(req, id);
      checkCapacity(id);
      return cacheSession(id, pair, age);
    })();
    recovering.set(id, pending);
    try {
      return await pending;
    } finally {
      recovering.delete(id);
    }
  }

  async function refresh(req, id, session, used) {
    if (getSession(id) !== session) return;
    if (session.refreshing) return session.refreshing;
    if (session.pair !== used) return;
    // The API revokes the login session if a consumed refresh token is reused.
    session.refreshing = (async () => {
      try {
        if (rememberedId.test(id)) {
          const { pair, age } = await rememberedAccess(req, id);
          session.pair = pair;
          session.until = Date.now() + age * 1000;
          return;
        }
        const reply = await upstream(req, '/v1/auth/refresh', {
          method: 'POST', body: JSON.stringify({ refresh_token: used.refresh }),
        });
        if (reply.status === 401 || reply.status === 403) {
          throw new SiteError(401, 'website_session_expired', '登录已过期，请重新登录。');
        }
        if (reply.status !== 200) throw gatewayError();
        session.pair = tokens(replyJson(reply));
      } catch (err) {
        // A timeout may occur after rotation, so discard the old pair even then.
        dropSession(id);
        throw err;
      }
    })();
    try {
      await session.refreshing;
    } finally {
      session.refreshing = null;
    }
  }

  async function withSession(req, id, path, options = {}) {
    const session = await ensureSession(req, id);
    // An <img> request uses the session cookie. Approved photos are shared across authorized viewers.
    const sharedPhoto = req.method === 'GET' && photoImagePath.test(path);
    const todoCallbackRequest = req.method === 'GET' && path === todoPath + '/finish';
    if (!options.adminSession && path !== '/v1/me' && !sharedPhoto && !todoCallbackRequest) checkContext(req, session);
    if (session.refreshing) await session.refreshing;
    const pair = getSession(id).pair;
    const reply = await upstream(req, path, { ...options, access: pair.access });
    const expired = result => result.status === 401 && (
      !options.adminSession || replyJson(result)?.error?.code === 'admin_site_session_invalid'
    );
    if (!expired(reply)) return reply;
    await refresh(req, id, session, pair);
    const retry = await upstream(req, path, { ...options, access: getSession(id).pair.access });
    if (expired(retry)) dropSession(id);
    return retry;
  }

  function replyCalendar(req, res, reply, download = false) {
    res.setHeader('X-Robots-Tag', 'noindex, nofollow, noarchive');
    if (reply.status === 200 || reply.status === 304) {
      const etag = reply.headers.get('etag');
      if (etag && /^(W\/)?"[0-9a-f]{64}"$/.test(etag)) res.setHeader('ETag', etag);
      if (reply.status === 304) return answer(res, 304);
      if (reply.headers.get('content-type')?.split(';')[0] !== 'text/calendar') throw gatewayError();
      res.setHeader('Content-Type', 'text/calendar; charset=utf-8');
      if (download) res.setHeader('Content-Disposition', 'attachment; filename="campus-schedule.ics"');
      const length = reply.headers.get('content-length');
      if (req.method === 'HEAD' && length && /^\d{1,8}$/.test(length)) res.setHeader('Content-Length', length);
      return answer(res, 200, reply.body);
    }
    return replyToBrowser(res, reply);
  }

  async function androidDownload(req, res, path) {
    const isQr = path === QR_PATH;
    const upstream = new URL(path, androidUrl);
    if (isQr) {
      if (!qrOrigin(publicUrl.origin)) throw new SiteError(404, 'download_qr_unavailable', '请使用手机可访问的 HTTPS 网站地址获取下载二维码。');
      upstream.searchParams.set('origin', publicUrl.origin);
    }
    const unavailable = new SiteError(503, 'android_download_unavailable', '安卓版安装包暂不可下载，请稍后重试。');
    let response;
    try {
      response = await fetch(upstream, {
        method: req.method, redirect: 'error', signal: AbortSignal.timeout(20_000),
        headers: { 'Accept-Encoding': 'identity' },
      });
    } catch {
      throw unavailable;
    }
    const length = Number(response.headers.get('content-length'));
    const media = isQr ? 'image/svg+xml' : APK_TYPE;
    if (response.status !== 200 || response.headers.get('content-type') !== media || !Number.isSafeInteger(length) ||
        length < 1 || length > (isQr ? 128 * 1024 : MAX_RELEASE_BYTES)) {
      await response.body?.cancel();
      throw unavailable;
    }
    res.setHeader('Content-Length', length);
    res.setHeader('Content-Type', media);
    if (!isQr) res.setHeader('Content-Disposition', `attachment; filename="${APK_FILENAME}"`);
    if (req.method === 'HEAD') return res.end();
    return pipeline(response.body, res);
  }

  async function todoAuthorization(req, res, id) {
    const reply = await withSession(req, id, todoPath + '/start', { body: '{}' });
    if (reply.status !== 200) return replyToBrowser(res, reply);
    let destination;
    try {
      destination = new URL(replyJson(reply).authorization_url);
      if (destination.origin !== 'https://login.microsoftonline.com' || destination.username || destination.password ||
          destination.hash || destination.pathname !== '/common/oauth2/v2.0/authorize') throw gatewayError();
    } catch {
      throw gatewayError();
    }
    if (destination.searchParams.get('redirect_uri') !== new URL(todoCallback, publicUrl).href) {
      throw new SiteError(409, 'todo_wrong_origin', '请从已配置的校园助手地址发起 Microsoft To Do 授权。');
    }
    return json(res, 200, { authorization_url: destination.href });
  }

  async function finishTodoAuthorization(req, res, id, params) {
    let result = 'failed';
    const state = params.get('state');
    const code = params.get('code');
    if (params.get('error') === 'access_denied') result = 'cancelled';
    if (!params.has('error') && state?.length === 43 && code?.length > 0 && code.length <= 4096 &&
        params.getAll('state').length === 1 && params.getAll('code').length === 1) {
      try {
        const reply = await withSession(req, id, todoPath + '/finish', {
          method: 'POST', body: JSON.stringify({ state, code }),
        });
        if (reply.status === 204) result = 'connected';
      } catch {
        // Always remove Microsoft's authorization parameters from the browser URL.
      }
    }
    return answer(res, 303, '', { Location: `/?todo=${result}` });
  }

  async function digestEvents(req, res, url, id) {
    if (url.search) throw new SiteError(422, 'invalid_digest_query', '更新订阅无需查询参数。');
    const session = await ensureSession(req, id);
    checkContext(req, session);
    if (res.destroyed) return;
    const connection = new AbortController();
    (session.digestStreams ??= new Set()).add(connection);
    const abort = () => connection.abort();
    res.once('close', abort);
    try {
      const reply = await withSession(req, id, digestEventsPath, { events: connection.signal });
      if (res.destroyed) return;
      if (!reply.stream) return replyToBrowser(res, reply);
      if (connection.signal.aborted || sessions.get(id) !== session) {
        await reply.stream.cancel().catch(() => {});
        throw new SiteError(401, 'website_session_expired', '登录已过期，请重新登录。');
      }
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-store, no-transform',
        'X-Accel-Buffering': 'no',
      });
      res.flushHeaders();
      await pipeline(reply.stream, res, { signal: connection.signal });
    } catch (error) {
      if (res.destroyed) return;
      if (connection.signal.aborted) throw new SiteError(401, 'website_session_expired', '登录已过期，请重新登录。');
      throw error;
    } finally {
      res.removeListener('close', abort);
      session.digestStreams.delete(connection);
    }
  }

  async function campusStart(req, res, id) {
    let data;
    try {
      data = JSON.parse(await readBody(req));
    } catch (err) {
      if (err instanceof SiteError) throw err;
      throw new SiteError(422, 'invalid_request', '校园登录参数无效。');
    }
    if (!data || !['login', 'link'].includes(data.mode)) {
      throw new SiteError(422, 'invalid_request', '校园登录参数无效。');
    }
    const verifier = randomBytes(32).toString('base64url');
    const payload = { mode: data.mode, code_challenge: createHash('sha256').update(verifier).digest('base64url') };
    if (casPublicUrl) payload.website_origin = publicUrl.origin;
    if (data.mode === 'link') payload.current_password = data.current_password;
    const body = JSON.stringify(payload);
    const reply = data.mode === 'link'
      ? await withSession(req, id, startPath, { body })
      : await upstream(req, startPath, { body });
    if (reply.status !== 200) return replyToBrowser(res, reply);

    const { authorization_url: address, local_cas: localCas } = replyJson(reply) || {};
    let destination;
    try {
      destination = new URL(address);
      if (destination.username || destination.password ||
          (localCas !== true && destination.protocol !== 'https:')) throw gatewayError();
    } catch {
      throw gatewayError();
    }
    if (localCas === true && (destination.origin !== (casPublicUrl || publicUrl).origin ||
        destination.pathname !== '/cas/login' || destination.hash)) {
      throw new SiteError(409, 'campus_wrong_origin', '请使用已配置的网站地址发起校园登录。');
    }
    const binding = reply.headers.getSetCookie().filter(value => cloudCookies.some(name => value.startsWith(`${name}=`)));
    if (binding.length !== 1) throw gatewayError();
    json(res, 200, { authorization_url: destination.href }, {
      'Set-Cookie': [...binding, cookie(flowName, verifier, 600, secure, flowSameSite)],
    });
  }

  async function campusCallback(req, res, id, url) {
    const cookies = [cookie(flowName, '', 0, secure, flowSameSite)];
    let outcome = 'failed';
    try {
      const verifier = cookieValue(req, flowName);
      const binding = cloudCookies.map(name => [name, cookieValue(req, name)])
        .filter(([, value]) => value).map(([name, value]) => `${name}=${value}`).join('; ');
      if (verifier.length !== 43 || !binding) throw new Error('Missing SSO browser binding');
      const body = req.method === 'POST' ? await readBody(req, formMedia, 262144) : undefined;
      const reply = await upstream(req, callbackPath + url.search, { binding, body, media: formMedia });
      if (reply.status !== 200) throw new Error('SSO callback rejected');
      const { completion_code: code, mode } = replyJson(reply) || {};
      if (typeof code !== 'string' || !code || !['login', 'link'].includes(mode)) throw gatewayError();
      if (mode === 'login') checkCapacity(id);
      const finished = await upstream(req, '/v1/auth/sso/campus/finish', {
        method: 'POST', body: JSON.stringify({ completion_code: code, code_verifier: verifier }),
      });
      if (finished.status === 200 && mode === 'login') {
        cookies.push(signIn(id, finished));
        outcome = 'signed-in';
      } else if (finished.status === 200 && replyJson(finished)?.linked === true) {
        outcome = 'linked';
      } else if (replyJson(finished)?.error?.code === 'account_link_required') {
        outcome = 'link-required';
      }
    } catch {
      // Redirect away from the credential-bearing callback URL, including on failure.
    }
    for (const name of cloudCookies) cookies.push(cookie(name, '', 0, secure, flowSameSite));
    answer(res, 303, '', { Location: `/?campus=${outcome}`, 'Set-Cookie': cookies });
  }

  function casBinding(req) {
    return casCookies.map(name => [name, cookieValue(req, name)])
      .filter(([, value]) => value).map(([name, value]) => `${name}=${value}`).join('; ');
  }

  async function casRequest(req, path, query = '', body = undefined, timeout = 10_000) {
    const headers = new Headers({ 'x-cas-site-origin': publicUrl.origin });
    const binding = casBinding(req);
    const address = clientAddress(req);
    if (binding) headers.set('cookie', binding);
    if (address) headers.set('x-cas-client-ip', address);
    if (req.method === 'POST') headers.set('origin', publicUrl.origin);
    if (body !== undefined) headers.set('content-type', formMedia);
    const signal = AbortSignal.timeout(timeout);
    try {
      return await fetch(new URL(path + query, casUrl), { method: req.method, headers, body, signal, redirect: 'manual' });
    } catch {
      throw new SiteError(signal.aborted ? 504 : 502, 'campus_cas_unavailable', '校园认证暂不可用，请稍后重试。');
    }
  }

  async function campusCas(req, res, url) {
    const path = url.pathname;
    const body = req.method === 'POST' && path === '/cas/login' ? await readBody(req, formMedia, 4096) : undefined;
    const reply = await casRequest(req, path, url.search, body);
    const headers = {};
    const cookies = reply.headers.getSetCookie().filter(value => casCookies.some(name => value.startsWith(`${name}=`)));
    if (cookies.length) headers['Set-Cookie'] = cookies;
    if (reply.status === 303 && reply.headers.has('location')) {
      const location = new URL(reply.headers.get('location'), publicUrl);
      const expected = path === '/cas/login' ? callbackPath : path === '/cas/logout' ? '/' : '';
      if (location.origin !== publicUrl.origin || location.username || location.password ||
          !expected || location.pathname !== expected) {
        await reply.body?.cancel();
        throw gatewayError();
      }
      await reply.body?.cancel();
      return answer(res, 303, '', { ...headers, Location: location.pathname + location.search });
    }
    const media = reply.headers.get('content-type');
    const expected = path.endsWith('.css') ? 'text/css' : path === '/cas/password.js' ? 'text/javascript' : 'text/html';
    if (media?.split(';')[0] !== expected) {
      await reply.body?.cancel();
      throw gatewayError();
    }
    const retry = reply.headers.get('retry-after');
    if (retry) headers['Retry-After'] = retry;
    const csp = reply.headers.get('content-security-policy');
    if (csp) headers['Content-Security-Policy'] = csp;
    const referrer = reply.headers.get('referrer-policy');
    if (referrer) headers['Referrer-Policy'] = referrer;
    answer(res, reply.status, Buffer.from(await reply.arrayBuffer()), { ...headers, 'Content-Type': media });
  }

  function adminCookies(reply) {
    return reply.headers.getSetCookie().filter(value => value.startsWith(`${adminSessionName}=`));
  }

  async function adminLogin(req, res, id, binding) {
    checkCapacity(id);
    const raw = await readBody(req, 'application/json', 4096);
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      throw new SiteError(422, 'invalid_request', '请填写邮箱和密码。');
    }
    if (typeof data?.email !== 'string' || typeof data?.password !== 'string') {
      throw new SiteError(422, 'invalid_request', '请填写邮箱和密码。');
    }
    const passwordReply = await upstream(req, '/v1/auth/login', {
      body: JSON.stringify({ email: data.email, password: data.password }),
    });
    if (passwordReply.status !== 200) return replyToBrowser(res, passwordReply);
    const credentials = replyJson(passwordReply);
    const { access } = tokens(credentials);
    const adminSession = randomBytes(32).toString('base64url');
    let signedIn = false;
    let reply;
    try {
      reply = await upstream(req, '/admin/api/login', { binding, access, adminSession });
      if (reply.status === 200) {
        const cookies = adminCookies(reply);
        if (cookies.length !== 1 || credentials.remember_token !== undefined) throw gatewayError();
        cookies.push(signIn(id, passwordReply, true, adminSession));
        res.setHeader('Set-Cookie', cookies);
        signedIn = true;
      }
    } finally {
      if (!signedIn) {
        await upstream(req, '/v1/auth/logout', { method: 'POST', access }).catch(() => {});
      }
    }
    return replyToBrowser(res, reply);
  }

  async function adminRequest(req, res, url, id) {
    if (!adminBridge || !loopback.has(uimUrl.hostname)) {
      throw new SiteError(503, 'admin_unavailable', '请通过主管理器启动网站和统一接口。');
    }
    const path = url.pathname;
    const value = cookieValue(req, adminSessionName);
    const binding = value ? `${adminSessionName}=${value}` : undefined;
    if (path === '/admin/api/login') {
      if (req.method !== 'POST') throw new SiteError(405, 'method_not_allowed', '请通过登录表单提交。');
      if (url.search) throw new SiteError(422, 'invalid_query', '登录接口无需查询参数。');
      return adminLogin(req, res, id, binding);
    }
    if (!value || !id) throw new SiteError(401, 'admin_login_required', '请先登录在线管理台。');
    getSession(id);
    const emptyAction = path === '/admin/api/logout' || path === '/admin/api/digest/weread/login' ||
      (path.startsWith('/admin/api/data/') && /\/(revoke|close|reconcile)$/.test(path));
    const hasBody = ['POST', 'PATCH', 'PUT'].includes(req.method) && !emptyAction;
    const media = 'application/json';
    const limit = path === '/admin/api/rag/documents' ? 32 * 1024 * 1024 : 65536;
    const body = hasBody ? await readBody(req, media, limit) : undefined;
    const reply = await withSession(req, id, path + url.search, { adminSession: id, binding, body, media });
    if (path === '/admin/api/logout' && reply.status === 204) {
      const cookies = adminCookies(reply);
      if (cookies.length === 1) res.setHeader('Set-Cookie', cookies);
    }
    return replyToBrowser(res, reply, adminPhotoImagePath.test(path) ? 'image/jpeg' : undefined);
  }

  async function rumorAdminRequest(req, res, url, id) {
    if (!adminBridge || !loopback.has(uimUrl.hostname)) {
      throw new SiteError(503, 'admin_unavailable', '请通过主管理器启动网站和统一接口。');
    }
    const value = cookieValue(req, adminSessionName);
    if (!value || !id) {
      if (req.method === 'GET' && !url.pathname.startsWith(rumorAdminPath + 'api/')) {
        return answer(res, 303, '', { Location: '/admin' });
      }
      throw new SiteError(401, 'admin_login_required', '请先登录在线管理台。');
    }
    const writing = req.method !== 'GET';
    if (writing && req.headers['x-campus-request'] !== '1') {
      throw new SiteError(403, 'admin_csrf_denied', '管理请求校验失败，请刷新页面。');
    }
    const hasBody = writing && (req.headers['transfer-encoding'] || Number(req.headers['content-length']) > 0);
    const media = req.headers['content-type'] || 'application/json';
    const upload = ['api/upload/evidence', 'api/radar/upload', 'api/tips'].some(path => url.pathname === rumorAdminPath + path);
    const allowedMedia = upload && media.split(';')[0].trim().toLowerCase() === 'multipart/form-data'
      ? 'multipart/form-data' : 'application/json';
    const body = hasBody ? await readBody(req, allowedMedia, upload ? 17 * 1024 * 1024 : 65536) : undefined;
    const reply = await withSession(req, id, url.pathname + url.search, {
      binding: `${adminSessionName}=${value}`, adminSession: id, rumorAdmin: true, media, body,
    });
    if (!writing && reply.status === 401 && !url.pathname.startsWith(rumorAdminPath + 'api/')) {
      return answer(res, 303, '', { Location: '/admin' });
    }
    const location = reply.headers.get('location');
    if ([301, 302, 303, 307, 308].includes(reply.status) && location?.startsWith(rumorAdminPath)) {
      return answer(res, reply.status, '', { Location: location });
    }
    if (reply.status === 204) return answer(res, 204, '');
    const contentType = reply.headers.get('content-type');
    if (!rumorAdminMedia.has(contentType?.split(';')[0])) throw gatewayError();
    const headers = { 'Content-Type': contentType };
    for (const header of ['content-security-policy', 'content-disposition']) {
      if (reply.headers.has(header)) headers[header] = reply.headers.get(header);
    }
    return answer(res, reply.status, reply.body, headers);
  }

  return createServer({ requestTimeout: 30_000, headersTimeout: 10_000 }, async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'");
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    if (secure) res.setHeader('Strict-Transport-Security', 'max-age=31536000');
    try {
      if (req.headers.host?.toLowerCase() !== publicUrl.host) {
        throw new SiteError(421, 'website_wrong_host', '请使用配置的网站地址访问。');
      }
      if (!req.url.startsWith('/') || req.url.startsWith('//') || req.url.includes('\\')) {
        throw new SiteError(400, 'invalid_request', '请求地址无效。');
      }
      const url = new URL(req.url, publicUrl);
      const path = url.pathname;
      const method = req.method;
      const id = cookieValue(req, sessionName);
      if ((method === 'GET' || method === 'HEAD') && assets.has(path)) {
        const [name, type] = assets.get(path);
        const body = await readFile(new URL(name, client)).catch(error => {
          if (error.code === 'ENOENT') throw new SiteError(404, 'asset_unavailable', '此资源尚未配置。');
          throw error;
        });
        return answer(res, 200, method === 'HEAD' ? undefined : body, { 'Content-Type': type });
      }
      if (method === 'GET' && path === '/health') return json(res, 200, { status: 'ok', service: 'user-website' });
      if (path === APK_PATH || path === QR_PATH) {
        if (url.search) throw new SiteError(404, 'download_not_found', '下载资源不存在。');
        if (method !== 'GET' && method !== 'HEAD') {
          res.setHeader('Allow', 'GET, HEAD');
          throw new SiteError(405, 'method_not_allowed', '仅支持读取下载资源。');
        }
        return await androidDownload(req, res, path);
      }
      if (path === callbackPath && ['GET', 'POST'].includes(method)) return await campusCallback(req, res, id, url);
      if (method === 'GET' && path === todoCallback) return await finishTodoAuthorization(req, res, id, url.searchParams);
      const feed = ['GET', 'HEAD'].includes(method) && calendarFeedPath.exec(path);
      if (feed && !url.search) {
        const reply = await upstream(req, '/v1/calendar/feed.ics', { access: feed[1], calendar: true });
        return replyCalendar(req, res, reply);
      }
      if (method !== 'GET' && !(method === 'HEAD' && path === calendarExportPath) && req.headers.origin !== publicUrl.origin) {
        throw new SiteError(403, 'website_origin_denied', '请求来源与网站不一致。');
      }
      if (path === '/admin/rumor' && method === 'GET') return answer(res, 303, '', { Location: rumorAdminPath + 'admin' });
      if (path.startsWith(rumorAdminPath) && ['GET', 'POST', 'PATCH', 'PUT', 'DELETE'].includes(method)) {
        return await rumorAdminRequest(req, res, url, id);
      }
      if (adminPath.test(path) && ['GET', 'POST', 'PATCH', 'PUT', 'DELETE'].includes(method)) {
        return await adminRequest(req, res, url, id);
      }
      if (!casPublicUrl && casPaths.get(path)?.includes(method)) return await campusCas(req, res, url);
      if (method === 'GET' && ['/v1/auth/providers', metadataPath].includes(path)) {
        return replyToBrowser(res, await upstream(req, path), path === metadataPath ? 'application/samlmetadata+xml' : undefined);
      }
      if (method === 'POST' && path === startPath) return await campusStart(req, res, id);
      if (method === 'POST' && path === '/v1/auth/login') {
        checkCapacity(id);
        const reply = await upstream(req, path, { body: await readBody(req) });
        if (reply.status !== 200) return replyToBrowser(res, reply);
        return answer(res, 204, '', { 'Set-Cookie': signIn(id, reply, true) });
      }
      if (method === 'POST' && openPost.has(path)) {
        return replyToBrowser(res, await upstream(req, path, { body: await readBody(req) }));
      }
      if (method === 'GET' && path === digestEventsPath) return await digestEvents(req, res, url, id);
      if (method === 'POST' && path === todoPath + '/start') return await todoAuthorization(req, res, id);
      if (!userRoutes.some(([methods, pattern]) => methods.split('|').includes(method) && pattern.test(path))) {
        throw new SiteError(404, 'website_not_found', '页面或用户接口不存在。');
      }
      await ensureSession(req, id);
      const noBody = path.endsWith('/cancel') || path === '/v1/auth/logout';
      const photoUpload = method === 'POST' && photoGalleryPath.test(path);
      const media = photoUpload ? 'application/octet-stream' : 'application/json';
      const body = method === 'PATCH' || method === 'PUT' || (method === 'POST' && !noBody)
        ? await readBody(req, media, photoUpload ? photoUploadLimit : 65536) : undefined;
      const calendar = path === calendarExportPath;
      const query = ['GET', 'HEAD'].includes(method) || (method === 'DELETE' && calendarEventPath.test(path)) ? url.search : '';
      const reply = await withSession(req, id, path + query, { body, media, calendar });
      if (calendar) return replyCalendar(req, res, reply, true);
      if (path === '/v1/me' && reply.status === 200) {
        res.setHeader('X-Website-Context', getSession(id).context);
      }
      if (reply.status === 204 && ['/v1/auth/logout', '/v1/auth/password/change'].includes(path)) {
        dropSession(id);
        const cookies = [cookie(sessionName, '', 0, secure), cookie(flowName, '', 0, secure, flowSameSite)];
        if (cookieValue(req, adminSessionName)) cookies.push(cookie(adminSessionName, '', 0, secure, 'Strict'));
        if (casBinding(req)) {
          try {
            const response = await casRequest(req, '/cas/logout', '', undefined, 2000);
            await response.body?.cancel();
          } catch {
            // The site logout is already committed. Clear the browser cookies even if CAS is offline.
          }
          for (const name of casCookies.filter(name => name.startsWith('__Host-') === secure)) {
            cookies.push(cookie(name, '', 0, secure));
          }
        }
        res.setHeader('Set-Cookie', cookies);
      }
      return replyToBrowser(res, reply, photoImagePath.test(path) ? 'image/jpeg' : undefined);
    } catch (err) {
      const fault = err instanceof SiteError ? err : new SiteError(500, 'website_error', '网站服务暂不可用。');
      if (!res.headersSent) json(res, fault.status, { error: { code: fault.code, message: fault.message } });
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const config = settings();
  createWebsite(config).listen(config.port, config.host, () => console.log(`Local: ${config.publicUrl.origin}`));
}
