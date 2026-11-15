import express from 'express';
import { buildSchema, graphql, GraphQLError } from 'graphql';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { ClubError, managedActions } from './store.js';

const publicDirectory = fileURLToPath(new URL('../public/', import.meta.url));

const schema = buildSchema(`
  type Activity {
    id: ID!
    name: String!
    description: String!
    location: String!
    startsAt: String!
    endsAt: String!
    capacity: Int!
    remaining: Int!
    points: Int!
  }

  type Registration {
    id: ID!
    activityId: ID!
    activityName: String!
    participantName: String!
    createdAt: String!
  }

  type AgentTool {
    name: String!
    description: String!
    parameters: String!
  }

  type CallToolResult {
    content: String!
    isError: Boolean!
  }

  type Query {
    activities: [Activity!]!
    registrations: [Registration!]!
    agentTools: [AgentTool!]!
  }

  type Mutation {
    registerActivity(activityId: ID!, participantName: String!): Registration
    cancelRegistration(registrationId: ID!): Registration
    callTool(name: String!, argumentsJson: String!): CallToolResult!
  }
`);

const tools = [
  {
    name: 'club_list_activities',
    description: '查询社团活动、容量和剩余名额。',
    parameters: JSON.stringify({ type: 'object', properties: {}, additionalProperties: false }),
  },
  {
    name: 'club_register_activity',
    description: '使用活动 ID 和参与人姓名报名社团活动。',
    parameters: JSON.stringify({
      type: 'object',
      properties: {
        activity_id: { type: 'string', minLength: 1 },
        participant_name: { type: 'string', minLength: 1 },
      },
      required: ['activity_id', 'participant_name'],
      additionalProperties: false,
    }),
  },
  {
    name: 'club_cancel_registration',
    description: '使用报名记录 ID 取消社团活动报名。',
    parameters: JSON.stringify({
      type: 'object',
      properties: { registration_id: { type: 'string', minLength: 1 } },
      required: ['registration_id'],
      additionalProperties: false,
    }),
  },
];

function domainCall(callback) {
  try {
    return callback();
  } catch (error) {
    if (error instanceof ClubError) {
      throw new GraphQLError(error.message, {
        extensions: { code: error.code, retryable: error.retryable },
      });
    }
    throw error;
  }
}

function agentActivity(activity) {
  return {
    id: activity.id,
    name: activity.name,
    description: activity.description,
    location: activity.location,
    starts_at: activity.startsAt,
    ends_at: activity.endsAt,
    capacity: activity.capacity,
    remaining: activity.remaining,
    points: activity.points,
    ...(activity.clubId ? { club_id: activity.clubId } : {}),
  };
}

function agentRegistration(registration) {
  return {
    id: registration.id,
    registration_id: registration.id,
    activity_id: registration.activityId,
    activity_name: registration.activityName,
    participant_name: registration.participantName,
    created_at: registration.createdAt,
  };
}

function checkKeys(args, allowed) {
  if (!args || Array.isArray(args) || typeof args !== 'object') {
    throw new ClubError('INVALID_ARGUMENT', 'argumentsJson 必须编码一个 JSON 对象');
  }
  if (Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new ClubError('INVALID_ARGUMENT', `只允许参数：${allowed.join('、') || '无'}`);
  }
}

function executeTool(store, name, args, unknownToolCode) {
  if (name === 'club_list_activities') {
    checkKeys(args, []);
    return { activities: store.listActivities().map(agentActivity) };
  }
  if (name === 'club_register_activity') {
    checkKeys(args, ['activity_id', 'participant_name']);
    const registration = store.registerActivity({
      activityId: args.activity_id,
      participantName: args.participant_name,
    });
    return { registration: agentRegistration(registration) };
  }
  if (name === 'club_cancel_registration') {
    checkKeys(args, ['registration_id']);
    const registration = store.cancelRegistration({ registrationId: args.registration_id });
    return { registration: agentRegistration(registration) };
  }
  throw new ClubError(unknownToolCode, `未知工具：${name}`);
}

function agentError(error) {
  return {
    error: { code: error.code, message: error.message, retryable: error.retryable },
  };
}

function callTool(store, name, argumentsJson) {
  try {
    let args;
    try {
      args = JSON.parse(argumentsJson);
    } catch {
      throw new ClubError('INVALID_ARGUMENT', 'argumentsJson 不是有效 JSON');
    }

    const result = executeTool(store, name, args, 'INVALID_ARGUMENT');
    return { content: JSON.stringify(result), isError: false };
  } catch (error) {
    if (!(error instanceof ClubError)) {
      throw error;
    }
    return {
      content: JSON.stringify(agentError(error)),
      isError: true,
    };
  }
}

export function createApp({ store, managementToken } = {}) {
  const app = express();
  const rootValue = {
    activities: () => store.listActivities(),
    registrations: () => store.listRegistrations({ managed: false }),
    agentTools: () => tools,
    registerActivity: ({ activityId, participantName }) => domainCall(
      () => store.registerActivity({ activityId, participantName }),
    ),
    cancelRegistration: ({ registrationId }) => domainCall(
      () => store.cancelRegistration({ registrationId }),
    ),
    callTool: ({ name, argumentsJson }) => callTool(store, name, argumentsJson),
  };

  app.use(express.json());
  const protectedHeader = managementToken && Buffer.from(`Bearer ${managementToken}`);
  function managed(request, response, next) {
    const received = Buffer.from(request.get('authorization') || '');
    if (!protectedHeader || received.length !== protectedHeader.length || !timingSafeEqual(received, protectedHeader)) {
      return response.status(401).json({ error: { code: 'UNAUTHORIZED', message: '此入口需要服务凭据' } });
    }
    response.set('Cache-Control', 'no-store');
    next();
  }

  const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
  const idValid = value => typeof value === 'string' && uuid.test(value);
  const clubDirectory = (actorId, viewer) => ({
    clubs: store.listClubs(actorId, viewer), archive_summary: store.archiveSummary(actorId),
  });
  app.get('/managed/clubs', managed, (req, res) => {
    if (!idValid(req.query.actor_id)) return res.status(400).json(agentError(new ClubError('INVALID_ARGUMENT', '账号无效')));
    return res.json(clubDirectory(req.query.actor_id));
  });
  app.get('/managed/capabilities', managed, (_req, res) => {
    res.json({ actions: managedActions });
  });
  function replyManaged(res, action) {
    try {
      return res.json(action());
    } catch (error) {
      if (!(error instanceof ClubError)) throw error;
      const statuses = { INVALID_ARGUMENT: 400, NOT_FOUND: 404, FORBIDDEN: 403, CONFLICT: 409, CLUB_REVIEW_REQUIRED: 403 };
      return res.status(statuses[error.code] || 400).json(agentError(error));
    }
  }
  app.get('/managed/registrations', managed, (req, res) => replyManaged(res, () => {
    const query = req.query.check_archives;
    const result = { registrations: store.listRegistrations().map(agentRegistration) };
    if (query === undefined) return result;
    const ids = typeof query === 'string' ? query.split(',') : [];
    if (!ids.length || ids.length > 100 || ids.some(id => !/^[a-zA-Z0-9_-]{1,96}$/.test(id))) {
      throw new ClubError('INVALID_ARGUMENT', '活动标识无效');
    }
    return { ...result, purged_activity_ids: store.purgedActivities(ids) };
  }));
  app.post('/managed/registrations', managed, (req, res) => replyManaged(res, () => {
    checkKeys(req.body, ['participants']);
    const { participants } = req.body;
    if (!Array.isArray(participants) || participants.length < 1 || participants.length > 128 ||
        participants.some(id => typeof id !== 'string' || !/^web-[a-f0-9]{32}$/.test(id))) {
      throw new ClubError('INVALID_ARGUMENT', '报名凭据无效');
    }
    return { registrations: store.registrationsFor(participants) };
  }));
  app.post('/managed/directory', managed, (req, res) => replyManaged(res, () => {
    if (!idValid(req.body?.actor_id)) throw new ClubError('INVALID_ARGUMENT', '账号无效');
    return clubDirectory(req.body.actor_id, req.body.viewer);
  }));
  app.post('/managed/archives/purge', managed, (req, res) => replyManaged(res, () => {
    const { actor_id: actorId, snapshot } = req.body ?? {};
    if (!idValid(actorId) || typeof snapshot !== 'string' || !/^[a-f0-9]{64}$/.test(snapshot)) {
      throw new ClubError('INVALID_ARGUMENT', '账号或归档快照无效');
    }
    return store.purgeArchives(actorId, snapshot);
  }));
  app.get('/managed/archives', managed, (req, res) => replyManaged(res, () => {
    const offset = req.query.offset ?? '0';
    if (typeof offset !== 'string' || !/^[0-9]{1,5}$/.test(offset)) throw new ClubError('INVALID_ARGUMENT', '分页无效');
    return store.archiveQueue(Number(offset));
  }));
  function changeArchive(req, action, owner) {
    const { version, activity_id: activityId } = req.body ?? {};
    if (!idValid(req.params.id) || (activityId !== undefined && !idValid(activityId))
        || !Number.isSafeInteger(version) || version < 1 || !['restore', 'purge'].includes(action)) {
      throw new ClubError('INVALID_ARGUMENT', '归档操作或版本无效');
    }
    return store.changeArchive(req.params.id, activityId, version, action, owner);
  }
  app.patch('/managed/archives/:id', managed, (req, res) => replyManaged(res, () => changeArchive(req, req.body?.action, null)));
  app.post('/managed/archives/:id/purge', managed, (req, res) => replyManaged(res, () => {
    const owner = req.body?.actor_id;
    if (!idValid(owner)) throw new ClubError('INVALID_ARGUMENT', '账号无效');
    return changeArchive(req, 'purge', owner);
  }));
  app.post('/managed/activities', managed, (req, res) => {
    res.json({ activities: store.listActivities(req.body).map(agentActivity) });
  });
  app.post('/managed/register', managed, (req, res) => replyManaged(res, () => {
    const data = req.body ?? {};
    const registration = store.registerActivity({ activityId: data.activity_id, participantName: data.participant_name, viewer: data.viewer });
    return { registration: agentRegistration(registration) };
  }));
  app.post('/managed/cancel', managed, (req, res) => replyManaged(res, () => {
    const data = req.body ?? {};
    if (typeof data.participant_name !== 'string' || !data.participant_name.trim()) {
      throw new ClubError('INVALID_ARGUMENT', '报名人标识无效');
    }
    return { registration: agentRegistration(store.cancelRegistration({ registrationId: data.registration_id,
      participantName: data.participant_name })) };
  }));
  app.get('/managed/review', managed, (req, res) => replyManaged(res, () => {
    const offset = req.query.offset ?? '0';
    if (typeof offset !== 'string' || !/^[0-9]{1,5}$/.test(offset)) throw new ClubError('INVALID_ARGUMENT', '分页无效');
    return store.reviewQueue(Number(offset));
  }));
  app.patch('/managed/review/:id', managed, (req, res) => replyManaged(res, () => {
    if (!idValid(req.params.id) || !Number.isSafeInteger(req.body?.version)) throw new ClubError('INVALID_ARGUMENT', '社团标识或版本无效');
    return { club: store.reviewClub(req.params.id, req.body.version, req.body.status) };
  }));
  app.post('/managed/clubs', managed, (req, res) => replyManaged(res, () => {
    const data = req.body;
    if (!idValid(data?.actor_id) || !idValid(data?.id)
        || (data.activity_id !== undefined && !idValid(data.activity_id))
        || (data.effect_id !== undefined && !idValid(data.effect_id))) {
      throw new ClubError('INVALID_ARGUMENT', '社团或账号标识无效');
    }
    const club = store.manageClub(data);
    return data.effect_id ? { club, effect: store.changeStatus(data.effect_id, data.actor_id) } : { club };
  }));
  app.get('/managed/effects/:id', managed, (req, res) => replyManaged(res, () => {
    if (!idValid(req.params.id) || !idValid(req.query.actor_id)) throw new ClubError('INVALID_ARGUMENT', '操作标识无效');
    return { effect: store.changeStatus(req.params.id, req.query.actor_id) };
  }));
  app.post('/managed/effects/:id', managed, (req, res) => replyManaged(res, () => {
    if (!idValid(req.params.id) || !idValid(req.body?.actor_id)) throw new ClubError('INVALID_ARGUMENT', '操作标识无效');
    return { effect: store.undoChange(req.params.id, req.body.actor_id) };
  }));
  app.get('/admin', (_req, res) => {
    res.sendFile('admin.html', { root: publicDirectory });
  });
  app.get('/healthz', (req, res) => {
    res.json({ status: 'ok', service: 'club' });
  });
  app.get('/agent/tools', (req, res) => {
    res.json({
      tools: tools.map((tool) => ({ ...tool, parameters: JSON.parse(tool.parameters) })),
    });
  });
  app.post('/agent/call', (req, res) => {
    try {
      const body = req.body;
      const keys = body && typeof body === 'object' && !Array.isArray(body)
        ? Object.keys(body).sort()
        : [];
      if (keys.length !== 2 || keys[0] !== 'arguments' || keys[1] !== 'name'
        || typeof body.name !== 'string'
        || !body.arguments || typeof body.arguments !== 'object' || Array.isArray(body.arguments)) {
        throw new ClubError('INVALID_ARGUMENT', '请求必须严格包含 name 字符串和 arguments 对象');
      }
      const result = executeTool(store, body.name, body.arguments, 'NOT_FOUND');
      res.json({ content: JSON.stringify(result) });
    } catch (error) {
      if (!(error instanceof ClubError)) {
        throw error;
      }
      const statuses = {
        INVALID_ARGUMENT: 400,
        NOT_FOUND: 404,
        CONFLICT: 409,
        CAPACITY_FULL: 409,
      };
      res.status(statuses[error.code]).json(agentError(error));
    }
  });

  app.post('/graphql', async (req, res) => {
    const { query, variables, operationName } = req.body ?? {};
    if (typeof query !== 'string') {
      res.status(400).json({
        errors: [{ message: '请求必须包含 GraphQL query 字符串' }],
      });
      return;
    }
    const result = await graphql({
      schema,
      source: query,
      rootValue,
      variableValues: variables,
      operationName,
    });
    res.json(result);
  });

  app.use(express.static(publicDirectory));
  app.use((error, req, res, next) => {
    if (req.path === '/agent/call' && error.type === 'entity.parse.failed') {
      res.status(400).json(agentError(new ClubError('INVALID_ARGUMENT', '请求正文不是有效 JSON')));
      return;
    }
    next(error);
  });

  return app;
}
