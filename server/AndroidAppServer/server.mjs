import { open } from 'node:fs/promises';
import { createServer } from 'node:http';
import { pipeline } from 'node:stream/promises';
import { pathToFileURL } from 'node:url';

import qrcode from './vendor/qrcode-generator-2.0.4.mjs';

export const APK_PATH = '/downloads/campus-calendar.apk';
export const QR_PATH = '/downloads/campus-calendar-qr.svg';
export const APK_FILENAME = 'cuc-campus-calendar.apk';
export const APK_TYPE = 'application/vnd.android.package-archive';
export const MAX_RELEASE_BYTES = 32 * 1024 * 1024;
export const DOWNLOAD_PORT = 8186;
const loopbackHosts = new Set(['localhost', '127.0.0.1', '[::1]']);

export function qrOrigin(value) {
  if (!value || value.length > 300) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.origin === value && !loopbackHosts.has(url.hostname) ? url : null;
  } catch {
    return null;
  }
}

function downloadQr(origin) {
  const code = qrcode(0, 'M');
  code.addData(new URL(APK_PATH, origin).href);
  code.make();
  return code.createSvgTag({ cellSize: 5, margin: 20 });
}

export function createAndroidDownloads({ apk = new URL(`releases/${APK_FILENAME}`, import.meta.url) } = {}) {
  return createServer({ requestTimeout: 15_000, headersTimeout: 10_000 }, async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    const head = req.method === 'HEAD';
    const respond = (status, type, body) => {
      res.writeHead(status, { 'Content-Type': type, 'Content-Length': Buffer.byteLength(body) });
      res.end(head ? undefined : body);
    };
    if (req.method !== 'GET' && !head) {
      res.setHeader('Allow', 'GET, HEAD');
      return respond(405, 'text/plain; charset=utf-8', '仅支持读取下载资源。');
    }
    let url;
    try { url = new URL(req.url, 'http://127.0.0.1'); }
    catch { return respond(400, 'text/plain; charset=utf-8', '请求地址无效。'); }
    if (url.pathname === '/health' && !url.search) return respond(200, 'application/json', '{"status":"ok"}');
    const origin = url.pathname === QR_PATH && url.searchParams.size === 1 && qrOrigin(url.searchParams.get('origin'));
    if (!origin && (url.pathname !== APK_PATH || url.search)) return respond(404, 'text/plain; charset=utf-8', '下载资源不存在。');

    let file;
    try {
      file = await open(apk, 'r');
      const release = await file.stat();
      if (!release.isFile() || release.size < 1 || release.size > MAX_RELEASE_BYTES) throw new Error('unavailable release');
      if (origin) return respond(200, 'image/svg+xml', downloadQr(origin));

      res.writeHead(200, {
        'Content-Type': APK_TYPE,
        'Content-Length': release.size,
        'Content-Disposition': `attachment; filename="${APK_FILENAME}"`,
      });
      if (head) res.end();
      else await pipeline(file.createReadStream({ start: 0, end: release.size - 1, autoClose: false }), res);
    } catch {
      if (res.headersSent) res.destroy();
      else respond(503, 'text/plain; charset=utf-8', '安卓版安装包暂未提供，请稍后再试。');
    } finally {
      await file?.close().catch(() => {});
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  createAndroidDownloads().listen(DOWNLOAD_PORT, '127.0.0.1', () => {
    process.stdout.write(`Android downloads on 127.0.0.1:${DOWNLOAD_PORT}\n`);
  });
}
