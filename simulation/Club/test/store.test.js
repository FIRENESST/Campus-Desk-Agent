import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';

const fixtures = [
  { id: 'zine', name: '独立刊物夜', startsAt: '2026-11-13T19:30', endsAt: '2026-11-13T21:00', capacity: 1 },
  { id: 'film', name: '旧城胶片漫步', startsAt: '2026-11-14T14:00', endsAt: '2026-11-14T16:30', capacity: 2 },
];

test('报名会原子地占用名额，取消会释放名额', () => {
  const store = createStore(fixtures);

  const registration = store.registerActivity({ activityId: 'zine', participantName: ' 阿禾 ' });
  assert.equal(registration.participantName, '阿禾');
  assert.equal(store.listActivities().find((activity) => activity.id === 'zine').remaining, 0);
  assert.equal(store.listRegistrations().length, 1);

  const cancelled = store.cancelRegistration({ registrationId: registration.id });
  assert.equal(cancelled.id, registration.id);
  assert.equal(store.listRegistrations().length, 0);
  assert.equal(store.listActivities().find((activity) => activity.id === 'zine').remaining, 1);
});

test('同人在同活动重复报名返回 CONFLICT 且不产生额外记录', () => {
  const store = createStore(fixtures);
  store.registerActivity({ activityId: 'film', participantName: '阿禾' });

  assert.throws(
    () => store.registerActivity({ activityId: 'film', participantName: '阿禾' }),
    { code: 'CONFLICT', retryable: false },
  );
  assert.equal(store.listRegistrations().length, 1);
  assert.equal(store.listActivities().find((activity) => activity.id === 'film').remaining, 1);
});

test('容量满返回 CAPACITY_FULL，连续写入也不会超卖', async () => {
  const store = createStore(fixtures);
  const results = await Promise.allSettled([
    Promise.resolve().then(() => store.registerActivity({ activityId: 'zine', participantName: '阿禾' })),
    Promise.resolve().then(() => store.registerActivity({ activityId: 'zine', participantName: '小满' })),
  ]);

  assert.deepEqual(results.map((result) => result.status), ['fulfilled', 'rejected']);
  assert.equal(results[1].reason.code, 'CAPACITY_FULL');
  assert.equal(store.listRegistrations().length, 1);
  assert.equal(store.listActivities().find((activity) => activity.id === 'zine').remaining, 0);
  assert.throws(
    () => store.registerActivity({ activityId: 'zine', participantName: '小满' }),
    { code: 'CAPACITY_FULL', retryable: false },
  );
});

test('未知活动或报名 ID 返回 NOT_FOUND', () => {
  const store = createStore(fixtures);

  assert.throws(
    () => store.registerActivity({ activityId: 'missing', participantName: '阿禾' }),
    { code: 'NOT_FOUND', retryable: false },
  );
  assert.throws(
    () => store.cancelRegistration({ registrationId: 'missing' }),
    { code: 'NOT_FOUND', retryable: false },
  );
});

test('空标识和空白参与人返回 INVALID_ARGUMENT', () => {
  const store = createStore(fixtures);

  for (const input of [
    { activityId: '', participantName: '阿禾' },
    { activityId: 'zine', participantName: '   ' },
    { activityId: 'zine', participantName: 7 },
  ]) {
    assert.throws(() => store.registerActivity(input), { code: 'INVALID_ARGUMENT', retryable: false });
  }
  assert.throws(() => store.cancelRegistration({ registrationId: ' ' }), { code: 'INVALID_ARGUMENT' });
  assert.equal(store.listRegistrations().length, 0);
});
