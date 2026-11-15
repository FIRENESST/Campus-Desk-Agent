import test, { afterEach, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createApp } from '../src/app.js';
import { createStore } from '../src/store.js';

let server;
let origin;
let store;

beforeEach(async () => {
  store = createStore([
    { id: 'zine', name: '独立刊物夜', description: '一起做一本小志', location: '编辑室',
      startsAt: '2026-11-13T19:30', endsAt: '2026-11-13T21:00', capacity: 1 },
    { id: 'film', name: '旧城胶片漫步', description: '沿街取景', location: '南门集合',
      startsAt: '2026-11-14T14:00', endsAt: '2026-11-14T16:30', capacity: 2 },
  ]);
  await new Promise((resolve) => {
    server = createApp({ store, managementToken: 'test-management-token' }).listen(0, '127.0.0.1', resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
});

afterEach(async () => {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  store.close();
});

test('社团目录按账号展示管理权限，只公开已发布活动', async () => {
  const owner = '00000000-0000-4000-8000-000000000001';
  const member = '00000000-0000-4000-8000-000000000002';
  const id = '00000000-0000-4000-8000-000000000003';
  const activityId = '00000000-0000-4000-8000-000000000004';
  const path = `${origin}/managed/clubs`;
  const headers = { authorization: 'Bearer test-management-token', 'content-type': 'application/json' };
  const discovery = `${origin}/managed/capabilities`;
  assert.equal((await fetch(discovery)).status, 401);
  const capabilities = await (await fetch(discovery, { headers })).json();
  assert.deepEqual(capabilities.actions, ['register', 'update', 'archive', 'publish_activity', 'update_activity', 'archive_activity',
    'restore', 'restore_activity']);
  const submit = data => fetch(path, { method: 'POST', headers, body: JSON.stringify({ id, actor_id: owner, ...data }) });
  const directory = async actor => (await (await fetch(`${path}?actor_id=${actor}`, { headers })).json()).clubs;
  assert.equal((await fetch(`${path}?actor_id=${owner}`)).status, 401);
  const register = { action: 'register', name: '摄影社', category: '艺术', description: '校园摄影', registrant: '阿禾' };
  assert.equal((await submit(register)).status, 200);
  assert.equal((await submit(register)).status, 200);
  const activity = { action: 'publish_activity', activity_id: activityId, name: '外拍',
    description: '校园外拍', location: '南门', starts_at: '2026-12-01T14:00', ends_at: '2026-12-01T16:00', capacity: 1 };
  await assertAgentError(await submit(activity), 403, 'CLUB_REVIEW_REQUIRED');
  assert.deepEqual(await directory(member), []);
  assert.equal((await directory(owner))[0].review_status, 'pending');
  const review = `${origin}/managed/review/${id}`;
  assert.equal((await fetch(review, { method: 'PATCH', headers, body: JSON.stringify({ version: 1, status: 'approved' }) })).status, 200);
  for (const end of [undefined, '2026-12-01T06:00Z', '2026-12-01T13:59+08:00', '2026-12-01']) {
    await assertAgentError(await submit({ ...activity, ends_at: end }), 400, 'INVALID_ARGUMENT');
  }
  assert.equal((await submit(activity)).status, 200);
  assert.equal((await submit(activity)).status, 200);
  const [publicClub] = await directory(member);
  assert.equal(publicClub.can_manage, false);
  assert.equal(publicClub.registrant, '阿禾');
  assert.equal(publicClub.activities[0].id, activityId);
  assert.equal(publicClub.activities[0].ends_at, activity.ends_at);
  const points = publicClub.activities[0].points;
  assert.ok(Number.isInteger(points) && points >= 1 && points <= 4);
  const effectId = randomUUID();
  const rescheduled = { ...activity, action: 'update_activity', effect_id: effectId, version: 1,
    starts_at: '2026-12-01T23:50', ends_at: '2026-12-01T16:20Z' };
  for (const end of [undefined, '2026-12-01T15:50:59Z']) {
    await assertAgentError(await submit({ ...rescheduled, ends_at: end }), 400, 'INVALID_ARGUMENT');
  }
  assert.equal((await submit(rescheduled)).status, 200);
  const publicActivities = await fetch(`${origin}/managed/activities`, { method: 'POST', headers, body: '{}' });
  const published = (await publicActivities.json()).activities.find(item => item.id === activityId);
  assert.equal(published.ends_at, rescheduled.ends_at);
  assert.equal(published.points, points);
  const undo = await fetch(`${origin}/managed/effects/${effectId}`, {
    method: 'POST', headers, body: JSON.stringify({ actor_id: owner }),
  });
  assert.deepEqual(await undo.json(), { effect: 'undone' });
  const restored = (await directory(member))[0].activities[0];
  assert.equal(restored.starts_at, activity.starts_at);
  assert.equal(restored.ends_at, activity.ends_at);
  assert.equal((await directory(owner))[0].can_manage, true);
  assert.equal((await submit({ action: 'archive', actor_id: member, version: 1 })).status, 403);
  const participant = `web-${randomUUID().replaceAll('-', '')}`;
  const booked = await fetch(`${origin}/managed/register`, { method: 'POST', headers, body: JSON.stringify({
    activity_id: activityId, participant_name: participant, viewer: { reviewed: false },
  }) });
  assert.equal(booked.status, 200);
  assert.equal((await directory(member))[0].activities[0].remaining, 0);
  assert.equal((await submit({ action: 'archive_activity', activity_id: activityId, version: restored.version })).status, 200);
  assert.deepEqual((await directory(member))[0].activities, []);
  assert.equal((await directory(owner))[0].activities[0].archived, true);
  store.registerActivity({ activityId: 'film', participantName: `web-${'a'.repeat(32)}` });
  const receipts = (authorized = true) => fetch(`${origin}/managed/registrations`, {
    method: 'POST', headers: authorized ? headers : { 'content-type': 'application/json' },
    body: JSON.stringify({ participants: [participant] }),
  });
  assert.equal((await receipts(false)).status, 401);
  const { registrations } = await (await receipts()).json();
  assert.deepEqual(registrations.map(row => [row.activity_id, row.starts_at, row.points]), [[activityId, activity.starts_at, points]]);
  store.cancelRegistration({ registrationId: registrations[0].id, participantName: participant });
  assert.deepEqual((await (await receipts()).json()).registrations, []);
});

test('社团登记人可按确认的清单永久清理归档，关联报名随之关闭', async () => {
  const [owner, other, current, retired, foreign] = Array.from({ length: 5 }, () => randomUUID());
  const [archivedActivity, retiredActivity, openActivity, effect] = Array.from({ length: 4 }, () => randomUUID());
  const headers = { authorization: 'Bearer test-management-token', 'content-type': 'application/json' };
  const managed = (path, data, method = 'POST') => fetch(`${origin}/managed/${path}`, {
    method, headers, body: JSON.stringify(data),
  });
  const club = (actor_id, id, data) => managed('clubs', { actor_id, id, ...data });
  const directory = async actor => (await (await fetch(`${origin}/managed/clubs?actor_id=${actor}`, { headers })).json());
  const purge = (actor_id, snapshot) => managed('archives/purge', { actor_id, snapshot });
  const fields = { name: '摄影社', category: '艺术', description: '校园摄影', registrant: '阿禾' };
  const activity = { name: '外拍', description: '校园外拍', location: '南门', starts_at: '2026-12-01T14:00',
    ends_at: '2026-12-01T16:00', capacity: 1 };
  for (const [actor, id] of [[owner, current], [owner, retired], [other, foreign]]) {
    assert.equal((await club(actor, id, { ...fields, action: 'register' })).status, 200);
  }
  for (const id of [current, retired]) {
    assert.equal((await managed(`review/${id}`, { version: 1, status: 'approved' }, 'PATCH')).status, 200);
  }
  for (const [id, activity_id] of [[current, archivedActivity], [retired, retiredActivity], [current, openActivity]]) {
    assert.equal((await club(owner, id, { ...activity, action: 'publish_activity', activity_id })).status, 200);
  }
  for (const activity_id of [archivedActivity, retiredActivity, openActivity]) {
    assert.equal((await managed('register', { activity_id, participant_name: 'participant', viewer: { reviewed: false } })).status, 200);
  }
  assert.equal((await club(owner, current, { action: 'archive_activity', activity_id: archivedActivity,
    version: 1, effect_id: effect })).status, 200);
  assert.equal((await club(other, foreign, { action: 'archive', version: 1 })).status, 200);
  const { snapshot: stale } = (await directory(owner)).archive_summary;
  assert.equal((await club(owner, retired, { action: 'archive', version: 2 })).status, 200);
  const { snapshot, ...counts } = (await directory(owner)).archive_summary;
  assert.deepEqual(counts, { club_count: 1, activity_count: 2 });
  assert.match(snapshot, /^[a-f0-9]{64}$/);
  assert.equal((await fetch(`${origin}/managed/archives/purge`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ actor_id: owner, snapshot }),
  })).status, 401);
  for (const response of [await purge(other, snapshot), await purge(owner, stale)]) {
    await assertAgentError(response, 409, 'CONFLICT');
  }
  assert.equal(store.listRegistrations().length, 3);

  const cleaned = await purge(owner, snapshot);
  assert.equal(cleaned.status, 200);
  assert.deepEqual(await cleaned.json(), counts);
  const [kept] = (await directory(owner)).clubs;
  assert.deepEqual([kept.id, ...kept.activities.map(item => item.id)], [current, openActivity]);
  assert.equal((await directory(other)).clubs.some(item => item.id === foreign && item.archived), true);
  const queried = [archivedActivity, retiredActivity, openActivity].join(',');
  const receipts = await fetch(`${origin}/managed/registrations?check_archives=${queried}`, { headers });
  assert.equal(receipts.status, 200);
  const { registrations, purged_activity_ids: purgedIds } = await receipts.json();
  assert.deepEqual(registrations.map(record => record.activity_id), [openActivity]);
  assert.deepEqual(purgedIds.sort(), [archivedActivity, retiredActivity].sort());

  await assertAgentError(await managed(`effects/${effect}`, { actor_id: owner }), 409, 'CONFLICT');
  await assertAgentError(await club(owner, retired, { ...fields, action: 'register' }), 409, 'CONFLICT');
  await assertAgentError(await club(owner, current, { ...activity, action: 'publish_activity', activity_id: archivedActivity }),
    409, 'CONFLICT');
  assert.equal((await club(owner, current, { action: 'archive_activity', activity_id: openActivity, version: 1 })).status, 200);
  await assertAgentError(await purge(owner, snapshot), 409, 'CONFLICT');
  assert.deepEqual((await directory(owner)).clubs[0].activities.map(item => [item.id, item.archived]), [[openActivity, true]]);
});

test('社团撤销按记录的先后关系恢复资料，并继续保护所有权和版本', async () => {
  const owner = '00000000-0000-4000-8000-000000000001';
  const other = '00000000-0000-4000-8000-000000000002';
  const id = '00000000-0000-4000-8000-000000000003';
  const created = '00000000-0000-4000-8000-000000000004';
  const edited = '00000000-0000-4000-8000-000000000005';
  const path = `${origin}/managed/clubs`;
  const headers = { authorization: 'Bearer test-management-token', 'content-type': 'application/json' };
  const submit = data => fetch(path, { method: 'POST', headers, body: JSON.stringify({ id, actor_id: owner, ...data }) });
  const undo = (effect, actor_id = owner) => fetch(`${origin}/managed/effects/${effect}`, {
    method: 'POST', headers, body: JSON.stringify({ actor_id }),
  });
  const fields = { name: '摄影社', category: '艺术', description: '校园摄影' };
  assert.equal((await submit({ ...fields, action: 'register', registrant: '阿禾', effect_id: created })).status, 200);
  assert.equal((await submit({ ...fields, action: 'update', name: '胶片社', version: 1, effect_id: edited })).status, 200);
  assert.equal((await undo(created)).status, 409);
  assert.equal((await undo(edited, other)).status, 404);
  for (let replay = 0; replay < 2; replay++) {
    const result = await undo(edited);
    assert.equal(result.status, 200);
    assert.deepEqual(await result.json(), { effect: 'undone' });
  }
  const [club] = (await (await fetch(`${path}?actor_id=${owner}`, { headers })).json()).clubs;
  assert.deepEqual([club.name, club.version], ['摄影社', 3]);
  assert.equal((await undo(created)).status, 200);
  assert.deepEqual((await (await fetch(`${path}?actor_id=${owner}`, { headers })).json()).clubs, []);
});

test('已核验且满足学院和年级的用户才能看到受限活动及报名', async () => {
  const owner = '00000000-0000-4000-8000-000000000001';
  const member = '00000000-0000-4000-8000-000000000002';
  const id = '00000000-0000-4000-8000-000000000003';
  const activity = '00000000-0000-4000-8000-000000000004';
  const headers = { authorization: 'Bearer test-management-token', 'content-type': 'application/json' };
  const managed = (path, data, method = 'POST') => fetch(`${origin}/managed/${path}`, { method, headers, body: JSON.stringify(data) });
  const eligible = { reviewed: true, college: '新闻学院', grade: 'ug2' };
  assert.equal((await managed('clubs', { action: 'register', id, actor_id: owner, name: '社团', category: '新闻', description: '介绍',
    registrant: '登记人', audience: { allow_grades: ['ug2'], allow_colleges: ['新闻学院'] } })).status, 200);
  assert.equal((await managed('review/' + id, { version: 1, status: 'approved' }, 'PATCH')).status, 200);
  assert.equal((await managed('clubs', { action: 'publish_activity', id, actor_id: owner, activity_id: activity,
    name: '采访', description: '介绍', location: '教学楼', starts_at: '2026-12-01T14:00',
    ends_at: '2026-12-01T16:00', capacity: 1 })).status, 200);
  assert.equal((await callTool('club_list_activities')).activities.some(item => item.id === activity), false);
  for (const viewer of [{ ...eligible, reviewed: false }, { ...eligible, grade: 'ug3' }, { ...eligible, college: '其他学院' }]) {
    assert.deepEqual((await (await managed('directory', { actor_id: member, viewer })).json()).clubs, []);
    assert.equal((await managed('register', { activity_id: activity, participant_name: member, viewer })).status, 404);
  }
  assert.equal((await (await managed('directory', { actor_id: member, viewer: eligible })).json()).clubs[0].id, id);
  const registered = await managed('register', { activity_id: activity, participant_name: member, viewer: eligible });
  assert.equal(registered.status, 200);
  const { registration } = await registered.json();
  assert.deepEqual((await graphql('query { registrations { id } }')).data.registrations, []);
  await assertAgentError(await postAgent({ name: 'club_cancel_registration', arguments: { registration_id: registration.id } }), 404, 'NOT_FOUND');
  assert.equal((await managed('cancel', { registration_id: registration.id, participant_name: owner })).status, 404);
  assert.equal((await managed('cancel', { registration_id: registration.id, participant_name: member })).status, 200);
  assert.equal((await managed('review/' + id, { version: 1, status: 'rejected' }, 'PATCH')).status, 409);
  assert.equal((await managed('review/' + id, { version: 2, status: 'rejected' }, 'PATCH')).status, 200);
  assert.deepEqual((await (await managed('directory', { actor_id: member, viewer: eligible })).json()).clubs, []);
  await assertAgentError(await managed('clubs', { action: 'publish_activity', id, actor_id: owner, activity_id: randomUUID(),
    name: '下一次采访', description: '介绍', location: '教学楼', starts_at: '2026-12-01T14:00',
    ends_at: '2026-12-01T16:00', capacity: 1 }),
  403, 'CLUB_REVIEW_REQUIRED');
});

test('登记人与后台可恢复和逐项永久删除归档，权限与版本仍生效', async () => {
  const [id, owner, other, activityId, effect] = Array.from({ length: 5 }, () => randomUUID());
  const headers = { authorization: 'Bearer test-management-token', 'content-type': 'application/json' };
  const send = (path, body, method = 'POST', auth = headers) => fetch(`${origin}/managed/${path}`, {
    method, headers: auth, body: JSON.stringify(body),
  });
  const club = (action, fields = {}) => send('clubs', { action, id, actor_id: owner, ...fields });
  const archive = 'archives/' + id;
  const queue = async () => (await (await fetch(origin + '/managed/archives', { headers })).json()).items;
  assert.equal((await club('register', { name: '测试社团', category: '艺术', description: '介绍', registrant: '阿禾' })).status, 200);
  assert.equal((await club('archive', { version: 1 })).status, 200);
  assert.deepEqual((await queue()).map(row => [row.club_id, row.activity_id, row.version]), [[id, null, 2]]);
  await assertAgentError(await club('restore', { actor_id: other, version: 2 }), 403, 'FORBIDDEN');
  await assertAgentError(await send(archive + '/purge', { actor_id: other, version: 2 }), 403, 'FORBIDDEN');
  const restored = await club('restore', { version: 2 });
  assert.equal(restored.status, 200);
  assert.equal((await restored.json()).club.review_status, 'pending');
  await assertAgentError(await send(archive + '/purge', { actor_id: owner, version: 2 }), 409, 'CONFLICT');
  assert.equal((await send('review/' + id, { version: 3, status: 'approved' }, 'PATCH')).status, 200);
  assert.equal((await club('publish_activity', { activity_id: activityId, name: '外拍', description: '活动介绍',
    location: '南门', starts_at: '2026-12-01T14:00', ends_at: '2026-12-01T16:00', capacity: 2 })).status, 200);
  assert.equal((await send('register', { activity_id: activityId, participant_name: 'participant', viewer: { reviewed: false } })).status, 200);
  const activity = (action, version, extra = {}) => club(action, { activity_id: activityId, version, ...extra });
  assert.equal((await activity('archive_activity', 1)).status, 200);
  assert.equal((await activity('restore_activity', 2, { effect_id: effect })).status, 200);
  assert.equal((await send('effects/' + effect, { actor_id: owner })).status, 200);
  const [row] = await queue();
  assert.equal(row.activity_id, activityId);
  assert.equal((await send(archive, { action: 'restore', activity_id: activityId, version: row.version },
    'PATCH', { 'content-type': 'application/json' })).status, 401);
  assert.equal((await send(archive, { action: 'restore', activity_id: activityId, version: row.version }, 'PATCH')).status, 200);
  assert.equal((await activity('archive_activity', row.version + 1)).status, 200);
  const removed = await send(archive + '/purge', { actor_id: owner, activity_id: activityId, version: row.version + 2 });
  assert.deepEqual(await removed.json(), { club_count: 0, activity_count: 1 });
  assert.equal(store.listRegistrations().length, 0);
  assert.deepEqual(store.purgedActivities([activityId]), [activityId]);
  assert.equal((await club('archive', { version: 4 })).status, 200);
  assert.equal((await send(archive, { action: 'restore', version: 5 }, 'PATCH')).status, 200);
  assert.equal((await club('archive', { version: 6 })).status, 200);
  const purged = await send(archive, { action: 'purge', version: 7 }, 'PATCH');
  assert.deepEqual(await purged.json(), { club_count: 1, activity_count: 0 });
  assert.deepEqual(await queue(), []);
});

async function postAgent(payload) {
  return fetch(`${origin}/agent/call`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

async function callTool(name, args = {}) {
  const response = await postAgent({ name, arguments: args });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(Object.keys(body), ['content']);
  assert.equal(typeof body.content, 'string');
  assert.equal(JSON.stringify(JSON.parse(body.content)), body.content);
  return JSON.parse(body.content);
}

async function assertAgentError(response, status, code) {
  assert.equal(response.status, status);
  const body = await response.json();
  assert.deepEqual(Object.keys(body), ['error']);
  assert.deepEqual(Object.keys(body.error).sort(), ['code', 'message', 'retryable']);
  assert.equal(body.error.code, code);
  assert.ok(body.error.message);
  assert.equal(typeof body.error.retryable, 'boolean');
}

async function graphql(query, variables = {}) {
  const response = await fetch(`${origin}/graphql`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  assert.equal(response.status, 200);
  return response.json();
}

test('HTTP healthz 返回 Club 健康状态', async () => {
  const response = await fetch(`${origin}/healthz`);

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok', service: 'club' });
});

test('HTTP /admin 返回运维管理台页面', async () => {
  const response = await fetch(`${origin}/admin`);

  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
  const html = await response.text();
  assert.ok(html.includes('社团 · 运维管理台'));
  assert.ok(html.includes('agent/call'));
});

test('HTTP 工具清单复用 GraphQL 工具且 parameters 为 JSON Schema 对象', async () => {
  const response = await fetch(`${origin}/agent/tools`);
  assert.equal(response.status, 200);
  const { tools } = await response.json();
  assert.deepEqual(tools.map((tool) => tool.name), [
    'club_list_activities',
    'club_register_activity',
    'club_cancel_registration',
  ]);

  const original = await graphql('query { agentTools { name description parameters } }');
  const expected = original.data.agentTools.map((tool) => ({
    ...tool,
    parameters: JSON.parse(tool.parameters),
  }));
  assert.deepEqual(tools, expected);
  for (const tool of tools) {
    assert.equal(typeof tool.description, 'string');
    assert.equal(typeof tool.parameters, 'object');
    assert.equal(Array.isArray(tool.parameters), false);
    assert.equal(tool.parameters.type, 'object');
  }
});

test('HTTP 三个工具完成查询、报名、取消并与 GraphQL 前端共享状态', async () => {
  const listed = await callTool('club_list_activities');
  assert.equal(listed.activities.length, 2);

  const registered = await callTool('club_register_activity', {
    activity_id: 'zine', participant_name: '阿禾',
  });
  const registration = registered.registration;
  const during = await graphql('query { activities { id remaining } registrations { id participantName } }');
  assert.equal(during.data.activities.find((activity) => activity.id === 'zine').remaining, 0);
  assert.equal(during.data.registrations[0].participantName, '阿禾');

  const listedDuring = await callTool('club_list_activities');
  assert.equal(listedDuring.activities.find((activity) => activity.id === 'zine').remaining, 0);
  const cancelled = await callTool('club_cancel_registration', { registration_id: registration.id });
  assert.equal(cancelled.registration.id, registration.id);
  const after = await graphql('query { activities { id remaining } registrations { id } }');
  assert.equal(after.data.activities.find((activity) => activity.id === 'zine').remaining, 1);
  assert.deepEqual(after.data.registrations, []);
  assert.equal(store.listRegistrations().length, 0);
});

test('HTTP 业务错误映射状态且 GraphQL 未知工具仍保持 isError 语义', async () => {
  const args = { activity_id: 'zine', participant_name: '阿禾' };
  await callTool('club_register_activity', args);
  await assertAgentError(await postAgent({ name: 'club_register_activity', arguments: args }), 409, 'CONFLICT');
  await assertAgentError(await postAgent({
    name: 'club_register_activity',
    arguments: { activity_id: 'zine', participant_name: '小满' },
  }), 409, 'CAPACITY_FULL');
  await assertAgentError(await postAgent({
    name: 'club_register_activity',
    arguments: { activity_id: 'missing', participant_name: '小满' },
  }), 404, 'NOT_FOUND');
  await assertAgentError(await postAgent({
    name: 'club_cancel_registration', arguments: { registration_id: 'missing' },
  }), 404, 'NOT_FOUND');
  await assertAgentError(await postAgent({
    name: 'club_register_activity',
    arguments: { activity_id: 'film', participant_name: ' ' },
  }), 400, 'INVALID_ARGUMENT');
  await assertAgentError(await postAgent({ name: 'club_unknown', arguments: {} }), 404, 'NOT_FOUND');

  const original = await graphql(`
    mutation { callTool(name: "club_unknown", argumentsJson: "{}") { content isError } }
  `);
  assert.equal(original.errors, undefined);
  assert.equal(original.data.callTool.isError, true);
  assert.equal(JSON.parse(original.data.callTool.content).error.code, 'INVALID_ARGUMENT');
  assert.equal(store.listRegistrations().length, 1);
});

test('HTTP 严格拒绝缺失或额外字段、非对象 arguments 和工具参数错误', async () => {
  const invalidPayloads = [
    {},
    { name: 'club_list_activities' },
    { arguments: {} },
    { name: 'club_list_activities', arguments: {}, extra: true },
    { name: 7, arguments: {} },
    { name: 'club_list_activities', arguments: [] },
    { name: 'club_list_activities', arguments: null },
    { name: 'club_list_activities', arguments: { extra: true } },
    { name: 'club_register_activity', arguments: { activity_id: 'zine' } },
  ];
  for (const payload of invalidPayloads) {
    await assertAgentError(await postAgent(payload), 400, 'INVALID_ARGUMENT');
  }

  const malformed = await fetch(`${origin}/agent/call`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: 'not-json',
  });
  await assertAgentError(malformed, 400, 'INVALID_ARGUMENT');
  assert.equal(store.listRegistrations().length, 0);
});
