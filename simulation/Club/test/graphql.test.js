import test, { afterEach, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { createApp } from '../src/app.js';

let server;
let endpoint;
let store;

beforeEach(async () => {
  store = createStore([
    { id: 'zine', name: '独立刊物夜', description: '一起做一本小志', location: '编辑室',
      startsAt: '2026-11-13T19:30', endsAt: '2026-11-13T21:00', capacity: 1 },
    { id: 'film', name: '旧城胶片漫步', description: '沿街取景', location: '南门集合',
      startsAt: '2026-11-14T14:00', endsAt: '2026-11-14T16:30', capacity: 2 },
  ]);
  const app = createApp({ store });
  await new Promise((resolve) => {
    server = app.listen(0, '127.0.0.1', resolve);
  });
  endpoint = `http://127.0.0.1:${server.address().port}/graphql`;
});

afterEach(async () => {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
});

async function graphql(query, variables = {}) {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  assert.equal(response.status, 200);
  return response.json();
}

const callToolMutation = `
  mutation CallTool($name: String!, $argumentsJson: String!) {
    callTool(name: $name, argumentsJson: $argumentsJson) { content isError }
  }
`;

test('域名 GraphQL 查询活动、报名并执行报名和取消', async () => {
  const initial = await graphql(`query {
    activities { id name description location startsAt endsAt capacity remaining }
    registrations { id activityId participantName }
  }`);
  assert.equal(initial.errors, undefined);
  assert.equal(initial.data.activities[0].endsAt, '2026-11-13T21:00');
  assert.equal(initial.data.activities[0].remaining, 1);
  assert.deepEqual(initial.data.registrations, []);

  const registered = await graphql(`mutation {
    registerActivity(activityId: "zine", participantName: "阿禾") {
      id activityId activityName participantName createdAt
    }
  }`);
  assert.equal(registered.errors, undefined);
  assert.equal(registered.data.registerActivity.participantName, '阿禾');

  const during = await graphql(`query { activities { id remaining } registrations { id } }`);
  assert.equal(during.data.activities.find((activity) => activity.id === 'zine').remaining, 0);
  assert.equal(during.data.registrations.length, 1);

  const registrationId = registered.data.registerActivity.id;
  const cancelled = await graphql(`mutation Cancel($id: ID!) {
    cancelRegistration(registrationId: $id) { id }
  }`, { id: registrationId });
  assert.equal(cancelled.errors, undefined);
  assert.equal(cancelled.data.cancelRegistration.id, registrationId);
  assert.equal(store.listRegistrations().length, 0);
});

test('域名业务失败包含稳定 code 和 retryable extensions', async () => {
  await graphql(`mutation { registerActivity(activityId: "zine", participantName: "阿禾") { id } }`);

  const duplicate = await graphql(`mutation { registerActivity(activityId: "zine", participantName: "阿禾") { id } }`);
  assert.equal(duplicate.errors[0].extensions.code, 'CONFLICT');
  assert.equal(duplicate.errors[0].extensions.retryable, false);

  const full = await graphql(`mutation { registerActivity(activityId: "zine", participantName: "小满") { id } }`);
  assert.equal(full.errors[0].extensions.code, 'CAPACITY_FULL');
  assert.equal(full.errors[0].extensions.retryable, false);

  const invalid = await graphql(`mutation { registerActivity(activityId: "zine", participantName: " ") { id } }`);
  assert.equal(invalid.errors[0].extensions.code, 'INVALID_ARGUMENT');
  assert.equal(store.listRegistrations().length, 1);
});

test('AgentTools 只发布三个约定工具且 parameters 是可解析 JSON', async () => {
  const body = await graphql(`query AgentTools { agentTools { name description parameters } }`);
  assert.equal(body.errors, undefined);
  assert.deepEqual(body.data.agentTools.map((tool) => tool.name), [
    'club_list_activities',
    'club_register_activity',
    'club_cancel_registration',
  ]);
  for (const tool of body.data.agentTools) {
    assert.equal(typeof tool.description, 'string');
    assert.ok(tool.description.length > 0);
    assert.equal(JSON.parse(tool.parameters).type, 'object');
  }
  const register = body.data.agentTools.find((tool) => tool.name === 'club_register_activity');
  assert.deepEqual(JSON.parse(register.parameters).required, ['activity_id', 'participant_name']);
  const cancel = body.data.agentTools.find((tool) => tool.name === 'club_cancel_registration');
  assert.deepEqual(JSON.parse(cancel.parameters).required, ['registration_id']);
});

test('CallTool 三个工具与前端域名接口共享同一份状态', async () => {
  const registered = await graphql(callToolMutation, {
    name: 'club_register_activity',
    argumentsJson: JSON.stringify({ activity_id: 'zine', participant_name: '阿禾' }),
  });
  assert.equal(registered.errors, undefined);
  assert.equal(registered.data.callTool.isError, false);
  const registration = JSON.parse(registered.data.callTool.content).registration;
  assert.equal(registration.participant_name, '阿禾');

  const visible = await graphql(`query { activities { id remaining } registrations { id participantName } }`);
  assert.equal(visible.data.activities.find((activity) => activity.id === 'zine').remaining, 0);
  assert.equal(visible.data.registrations[0].participantName, '阿禾');

  const listed = await graphql(callToolMutation, { name: 'club_list_activities', argumentsJson: '{}' });
  assert.equal(JSON.parse(listed.data.callTool.content).activities.find((activity) => activity.id === 'zine').remaining, 0);

  const cancelled = await graphql(callToolMutation, {
    name: 'club_cancel_registration',
    argumentsJson: JSON.stringify({ registration_id: registration.id }),
  });
  assert.equal(cancelled.errors, undefined);
  assert.equal(cancelled.data.callTool.isError, false);
  assert.equal(store.listRegistrations().length, 0);
  assert.equal(store.listActivities().find((activity) => activity.id === 'zine').remaining, 1);
});

test('CallTool 将重复、满员、未知 ID 作为 isError 业务结果而非 GraphQL 错误', async () => {
  await graphql(callToolMutation, {
    name: 'club_register_activity',
    argumentsJson: JSON.stringify({ activity_id: 'zine', participant_name: '阿禾' }),
  });
  const cases = [
    ['club_register_activity', { activity_id: 'zine', participant_name: '阿禾' }, 'CONFLICT'],
    ['club_register_activity', { activity_id: 'zine', participant_name: '小满' }, 'CAPACITY_FULL'],
    ['club_register_activity', { activity_id: 'missing', participant_name: '小满' }, 'NOT_FOUND'],
    ['club_cancel_registration', { registration_id: 'missing' }, 'NOT_FOUND'],
  ];

  for (const [name, args, code] of cases) {
    const body = await graphql(callToolMutation, { name, argumentsJson: JSON.stringify(args) });
    assert.equal(body.errors, undefined, `${code} 不应成为 GraphQL 传输错误`);
    assert.equal(body.data.callTool.isError, true);
    const content = JSON.parse(body.data.callTool.content);
    assert.equal(content.error.code, code);
    assert.equal(content.error.retryable, false);
  }
  assert.equal(store.listRegistrations().length, 1);
});

test('CallTool 对非法 JSON、空白参数和未知工具返回 INVALID_ARGUMENT 业务结果', async () => {
  const cases = [
    { name: 'club_register_activity', argumentsJson: '{bad json' },
    { name: 'club_register_activity', argumentsJson: JSON.stringify({ activity_id: 'zine', participant_name: ' ' }) },
    { name: 'club_unknown', argumentsJson: '{}' },
  ];

  for (const variables of cases) {
    const body = await graphql(callToolMutation, variables);
    assert.equal(body.errors, undefined);
    assert.equal(body.data.callTool.isError, true);
    assert.equal(JSON.parse(body.data.callTool.content).error.code, 'INVALID_ARGUMENT');
  }
  assert.equal(store.listRegistrations().length, 0);
});
