import { createHmac, randomBytes } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createApp } from './src/app.js';
import { createStore } from './src/store.js';

const host = process.env.CLUB_HOST || '127.0.0.1';
const port = Number(process.env.CLUB_PORT || 19002);
const data = new URL('./.data/', import.meta.url);
mkdirSync(data, { recursive: true });
const keyFile = new URL('management.key', data);
try {
  writeFileSync(keyFile, randomBytes(32), { flag: 'wx', mode: 0o600 });
} catch (error) {
  if (error.code !== 'EEXIST') throw error;
}
const key = readFileSync(keyFile);
if (key.length !== 32) throw new Error('社团管理密钥格式无效');
const managementToken = createHmac('sha256', key).update('minta-club-management-v1').digest('hex');
const store = createStore(undefined, { database: fileURLToPath(new URL('club.sqlite3', data)) });

createApp({ store, managementToken }).listen(port, host, () => {
  console.log(`社团服务已启动：http://${host}:${port}`);
});
