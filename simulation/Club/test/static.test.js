import test from 'node:test';
import assert from 'node:assert/strict';
import { createApp } from '../src/app.js';
import { createStore } from '../src/store.js';

test('同一 Express 服务托管中文原生前端及其 GraphQL 客户端', async () => {
  const server = createApp({ store: createStore() }).listen(0, '127.0.0.1');
  await new Promise((resolve) => server.once('listening', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;

  try {
    const page = await fetch(`${origin}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-type'), /text\/html/);
    const html = await page.text();
    assert.match(html, /社团编辑部/);
    assert.match(html, /<script defer src="\/app\.js"><\/script>/);

    const script = await fetch(`${origin}/app.js`);
    assert.equal(script.status, 200);
    assert.match(await script.text(), /fetch\('\/graphql'/);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
