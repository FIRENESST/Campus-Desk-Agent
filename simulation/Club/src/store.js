import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

function activityPoints(id) {
  return 1 + (createHash('sha256').update('campus-activity:').update(id).digest()[0] % 4);
}

export const managedActions = Object.freeze([
  'register', 'update', 'archive', 'publish_activity', 'update_activity', 'archive_activity',
  'restore', 'restore_activity',
]);

export class ClubError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ClubError';
    this.code = code;
    this.retryable = false;
  }
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new ClubError('INVALID_ARGUMENT', `${field} 必须是非空字符串`);
  }
  return value.trim();
}

function activityTimestamp(value, field) {
  const match = /^(\d{4}-\d{2}-\d{2})[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,9})?)?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$/
    .exec(requiredText(value, field));
  if (match) {
    const [, day, offset] = match;
    const midnight = Date.parse(`${day}T00:00:00Z`);
    const timestamp = Date.parse(match[0].replace(' ', 'T') + (offset ? '' : '+08:00'));
    if (Number.isFinite(midnight) && Number.isFinite(timestamp) && !day.startsWith('0000-')
        && new Date(midnight).toISOString().slice(0, 10) === day) return timestamp;
  }
  throw new ClubError('INVALID_ARGUMENT', `请提供有效的${field}（精确到分钟）`);
}

function checkActivityRange(start, end) {
  const begin = activityTimestamp(start, '开始时间');
  const finish = activityTimestamp(end, '结束时间');
  if (Math.floor(finish / 60_000) <= Math.floor(begin / 60_000)) {
    throw new ClubError('INVALID_ARGUMENT', '结束时间须晚于开始时间（精确到分钟）');
  }
}

function hasActivityRange({ starts_at, ends_at }) {
  try {
    checkActivityRange(starts_at, ends_at);
    return true;
  } catch (error) {
    if (error instanceof ClubError) return false;
    throw error;
  }
}

function clubAudience(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !['allow_grades', 'allow_colleges'].includes(key))) {
    throw new ClubError('INVALID_ARGUMENT', '社团开放范围无效');
  }
  const list = (values = []) => {
    if (!Array.isArray(values) || values.length > 100 || values.some(item => typeof item !== 'string' || !item.trim() || item.length > 100)) {
      throw new ClubError('INVALID_ARGUMENT', '社团开放范围无效');
    }
    return [...new Set(values.map(item => item.normalize('NFKC').trim().toLowerCase()))];
  };
  return { allow_grades: list(value.allow_grades), allow_colleges: list(value.allow_colleges) };
}

function visibleTo(audience, viewer) {
  const { allow_grades = [], allow_colleges = [] } = JSON.parse(audience);
  if (!allow_grades.length && !allow_colleges.length) return true;
  return viewer?.reviewed === true && (!allow_grades.length || allow_grades.includes(viewer.grade)) &&
    (!allow_colleges.length || allow_colleges.includes(viewer.college));
}

const schema = `
  PRAGMA foreign_keys=ON;
  PRAGMA busy_timeout=5000;
  CREATE TABLE IF NOT EXISTS clubs (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, registrant TEXT NOT NULL,
    name TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY, club_id TEXT REFERENCES clubs(id), name TEXT NOT NULL, description TEXT NOT NULL,
    location TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL CHECK (length(trim(ends_at)) > 0),
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    version INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id TEXT NOT NULL REFERENCES activities(id),
    activity_name TEXT NOT NULL, participant_name TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (activity_id, participant_name)
  );
  CREATE INDEX IF NOT EXISTS registrations_participant_idx ON registrations(participant_name);
  CREATE TABLE IF NOT EXISTS club_changes (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, club_id TEXT NOT NULL, activity_id TEXT,
    command TEXT NOT NULL, before_snapshot TEXT NOT NULL, after_version INTEGER NOT NULL,
    changed INTEGER NOT NULL, undone INTEGER NOT NULL DEFAULT 0
  );
  CREATE INDEX IF NOT EXISTS club_changes_target_idx ON club_changes(club_id,activity_id,after_version)
    WHERE changed=1 AND undone=0;
  CREATE TABLE IF NOT EXISTS club_archive_tombstones (
    kind TEXT NOT NULL CHECK (kind IN ('club','activity')), id TEXT NOT NULL,
    PRIMARY KEY (kind,id)
  );
`;

const activityQuery = `SELECT a.*, a.starts_at AS startsAt, a.ends_at AS endsAt,c.review_status,c.audience,
  a.capacity - (SELECT count(*) FROM registrations r WHERE r.activity_id=a.id) AS remaining
  FROM activities a LEFT JOIN clubs c ON c.id=a.club_id`;
const registrationQuery = `SELECT 'reg-'||id AS id,activity_id AS activityId,activity_name AS activityName,
  participant_name AS participantName,created_at AS createdAt FROM registrations`;

export function createStore(activities = [], { database = ':memory:' } = {}) {
  const db = new DatabaseSync(database);
  db.exec(schema);
  function transaction(change) {
    db.exec('BEGIN IMMEDIATE');
    try {
      const result = change();
      db.exec('COMMIT');
      return result;
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }

  transaction(() => {
    const columns = new Set(db.prepare('PRAGMA table_info(clubs)').all().map(row => row.name));
    if (!columns.has('review_status')) {
      db.exec("ALTER TABLE clubs ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected'))");
    }
    if (!columns.has('audience')) db.exec("ALTER TABLE clubs ADD COLUMN audience TEXT NOT NULL DEFAULT '{}'");
    const activityColumns = db.prepare('PRAGMA table_info(activities)').all();
    if (!activityColumns.some(column => column.name === 'ends_at')) db.exec('ALTER TABLE activities ADD COLUMN ends_at TEXT');
    const seed = db.prepare('INSERT OR IGNORE INTO activities(id,name,description,location,starts_at,ends_at,capacity) VALUES(?,?,?,?,?,?,?)');
    for (const { id, name, description = '', location = '', startsAt, endsAt, capacity } of activities) {
      checkActivityRange(startsAt, endsAt);
      seed.run(id, name, description, location, startsAt, endsAt, capacity);
    }
  });

  function getClub(id) {
    return db.prepare('SELECT * FROM clubs WHERE id=?').get(id);
  }

  function getActivity(clubId, activityId) {
    return db.prepare('SELECT * FROM activities WHERE club_id=? AND id=?').get(clubId, activityId);
  }

  function wasPurged(kind, id) {
    return db.prepare('SELECT 1 FROM club_archive_tombstones WHERE kind=? AND id=?').get(kind, id);
  }

  function archiveTargets(owner) {
    return db.prepare(`SELECT 'club' AS kind,id,version FROM clubs WHERE owner_id=? AND archived<>0
      UNION ALL
      SELECT 'activity',a.id,a.version FROM activities a JOIN clubs c ON c.id=a.club_id
      WHERE c.owner_id=? AND (c.archived<>0 OR a.archived<>0)
      ORDER BY kind,id`).all(owner, owner);
  }

  function summarizeArchives(owner, targets) {
    const clubCount = targets.filter(target => target.kind === 'club').length;
    const snapshot = createHash('sha256').update(JSON.stringify([owner, targets])).digest('hex');
    return { club_count: clubCount, activity_count: targets.length - clubCount, snapshot };
  }

  function getChange(id, owner) {
    const change = db.prepare('SELECT * FROM club_changes WHERE id=? AND actor_id=?').get(id, owner);
    if (!change) throw new ClubError('NOT_FOUND', '未找到该次社团操作');
    return change;
  }

  function checkOwner(club, owner) {
    if (!club) throw new ClubError('NOT_FOUND', '社团不存在');
    if (club.owner_id !== owner) throw new ClubError('FORBIDDEN', '只有社团登记人可以管理该社团');
  }

  function checkVersion(row, version, archived = false) {
    if (Boolean(row.archived) !== archived || row.version !== version) {
      throw new ClubError('CONFLICT', '记录已变更，请刷新后再操作');
    }
  }

  function setArchived(table, row, version, archived) {
    if (archived && row.archived) return;
    checkVersion(row, version, !archived);
    db.prepare(`UPDATE ${table} SET archived=?,version=version+1 WHERE id=?`).run(Number(archived), row.id);
  }

  function deleteArchives(clubs, activities) {
    const clubIds = JSON.stringify(clubs);
    const activityIds = JSON.stringify(activities);
    db.prepare(`INSERT OR IGNORE INTO club_archive_tombstones(kind,id)
      SELECT 'club',value FROM json_each(?) UNION ALL SELECT 'activity',value FROM json_each(?)`).run(clubIds, activityIds);
    // Keep effect receipts while discarding the deleted records' snapshots.
    db.prepare(`UPDATE club_changes SET command='',before_snapshot='null'
      WHERE club_id IN (SELECT value FROM json_each(?)) OR activity_id IN (SELECT value FROM json_each(?))`)
      .run(clubIds, activityIds);
    db.prepare('DELETE FROM registrations WHERE activity_id IN (SELECT value FROM json_each(?))').run(activityIds);
    db.prepare('DELETE FROM activities WHERE id IN (SELECT value FROM json_each(?))').run(activityIds);
    db.prepare('DELETE FROM clubs WHERE id IN (SELECT value FROM json_each(?))').run(clubIds);
    return { club_count: clubs.length, activity_count: activities.length };
  }

  function activityView({ id, name, description, location, startsAt, endsAt, capacity, remaining, version, archived }) {
    return { id, name, description, location, starts_at: startsAt, ends_at: endsAt, capacity, remaining,
      points: activityPoints(id), version, archived: Boolean(archived) };
  }

  function clubView(row, owner, viewer) {
    const canManage = row.owner_id === owner;
    const activities = db.prepare(`${activityQuery} WHERE a.club_id=? AND (?=1 OR a.archived=0) ORDER BY a.rowid`)
      .all(row.id, canManage ? 1 : 0);
    return { id: row.id, name: row.name, category: row.category, description: row.description,
      registrant: row.registrant, created_at: row.created_at, version: row.version,
      archived: Boolean(row.archived), can_manage: canManage,
      activities: activities.filter(activity => canManage || hasActivityRange(activity)).map(activityView),
      review_status: row.review_status, can_join: !row.archived && row.review_status === 'approved' && visibleTo(row.audience, viewer),
      ...(canManage ? { audience: clubAudience(JSON.parse(row.audience)) } : {}) };
  }

  function clubFields(data) {
    return [requiredText(data.name, 'name'), requiredText(data.category, 'category'), requiredText(data.description, 'description')];
  }

  function activityFields(data) {
    if (!Number.isInteger(data.capacity) || data.capacity < 1 || data.capacity > 10000) {
      throw new ClubError('INVALID_ARGUMENT', '活动容量须为 1 到 10000');
    }
    const start = requiredText(data.starts_at, '开始时间');
    const end = requiredText(data.ends_at, '结束时间');
    checkActivityRange(start, end);
    return [requiredText(data.name, 'name'), requiredText(data.description, 'description'),
      requiredText(data.location, 'location'), start, end, data.capacity];
  }

  return {
    close: () => db.close(),
    listActivities(viewer) {
      return db.prepare(`${activityQuery} WHERE a.archived=0 AND COALESCE(c.archived,0)=0
        AND (a.club_id IS NULL OR c.review_status='approved') ORDER BY a.rowid`).all()
        .filter(row => hasActivityRange(row) && (!row.club_id || visibleTo(row.audience, viewer)))
        .map(({ id, name, description, location, startsAt, endsAt, capacity, remaining, club_id }) => (
          { id, name, description, location, startsAt, endsAt, capacity, remaining, points: activityPoints(id),
            ...(club_id ? { clubId: club_id } : {}) }
        ));
    },

    listRegistrations({ managed = true } = {}) {
      return db.prepare(`${registrationQuery} WHERE ?=1 OR EXISTS (
        SELECT 1 FROM activities a WHERE a.id=registrations.activity_id AND a.club_id IS NULL
      ) ORDER BY registrations.id`).all(Number(managed)).map(row => ({ ...row }));
    },

    registrationsFor(participants) {
      return db.prepare(`SELECT 'reg-'||r.id AS id, r.activity_id, a.name AS activity_name,
        r.participant_name, a.starts_at FROM registrations r JOIN activities a ON a.id=r.activity_id
        WHERE r.participant_name IN (SELECT value FROM json_each(?))`)
        .all(JSON.stringify(participants)).map(row => ({ ...row, points: activityPoints(row.activity_id) }));
    },

    registerActivity({ activityId, participantName, viewer } = {}) {
      const validActivityId = requiredText(activityId, 'activity_id');
      const validParticipantName = requiredText(participantName, 'participant_name');
      return transaction(() => {
        const activity = db.prepare(`${activityQuery} WHERE a.id=? AND a.archived=0 AND COALESCE(c.archived,0)=0
          AND (a.club_id IS NULL OR c.review_status='approved')`).get(validActivityId);
        if (!activity || (activity.club_id && (!viewer || !visibleTo(activity.audience, viewer)))) {
          throw new ClubError('NOT_FOUND', '活动不存在或未向当前身份开放');
        }
        if (!hasActivityRange(activity)) throw new ClubError('CONFLICT', '活动的起止时间尚未确认，暂不能报名');
        if (db.prepare('SELECT 1 FROM registrations WHERE activity_id=? AND participant_name=?').get(validActivityId, validParticipantName)) {
          throw new ClubError('CONFLICT', `${validParticipantName} 已报名该活动`);
        }
        if (activity.remaining <= 0) throw new ClubError('CAPACITY_FULL', `活动「${activity.name}」名额已满`);
        const createdAt = new Date().toISOString();
        const { lastInsertRowid } = db.prepare('INSERT INTO registrations(activity_id,activity_name,participant_name,created_at) VALUES(?,?,?,?)')
          .run(activity.id, activity.name, validParticipantName, createdAt);
        return { id: `reg-${lastInsertRowid}`, activityId: activity.id, activityName: activity.name, participantName: validParticipantName, createdAt };
      });
    },

    cancelRegistration({ registrationId, participantName } = {}) {
      const id = requiredText(registrationId, 'registration_id');
      return transaction(() => {
        const registration = db.prepare(`${registrationQuery} WHERE 'reg-'||id=?`).get(id);
        const owned = registration && (participantName === undefined
          ? db.prepare('SELECT 1 FROM activities WHERE id=? AND club_id IS NULL').get(registration.activityId)
          : registration.participantName === participantName);
        if (!owned) throw new ClubError('NOT_FOUND', `报名记录 ${id} 不存在`);
        db.prepare("DELETE FROM registrations WHERE 'reg-'||id=?").run(id);
        return { ...registration };
      });
    },

    listClubs(owner, viewer) {
      return db.prepare("SELECT * FROM clubs WHERE (archived=0 AND review_status='approved') OR owner_id=? ORDER BY created_at DESC,id").all(owner)
        .filter(row => row.owner_id === owner || visibleTo(row.audience, viewer))
        .map(row => clubView(row, owner, viewer));
    },

    archiveSummary(owner) {
      return summarizeArchives(owner, archiveTargets(owner));
    },

    purgeArchives(owner, snapshot) {
      return transaction(() => {
        const targets = archiveTargets(owner);
        const { snapshot: current } = summarizeArchives(owner, targets);
        if (snapshot !== current) throw new ClubError('CONFLICT', '归档清单已更新，请刷新后重新确认');
        return deleteArchives(targets.filter(target => target.kind === 'club').map(target => target.id),
          targets.filter(target => target.kind === 'activity').map(target => target.id));
      });
    },

    archiveQueue(offset) {
      const rows = db.prepare(`
        SELECT c.id AS club_id,NULL AS activity_id,c.name AS club_name,NULL AS activity_name,c.registrant,c.version
        FROM clubs c WHERE c.archived=1
        UNION ALL
        SELECT c.id,a.id,c.name,a.name,c.registrant,a.version
        FROM activities a JOIN clubs c ON c.id=a.club_id WHERE a.archived=1 AND c.archived=0
        ORDER BY club_name,club_id,activity_name,activity_id LIMIT 41 OFFSET ?
      `).all(offset);
      return { items: rows.slice(0, 40), next_offset: rows.length > 40 ? offset + 40 : null };
    },

    changeArchive(id, activityId, version, action, owner) {
      if (!['restore', 'purge'].includes(action)) throw new ClubError('INVALID_ARGUMENT', '归档操作无效');
      return transaction(() => {
        const club = getClub(id);
        if (!club) throw new ClubError('NOT_FOUND', '社团不存在');
        if (owner !== null) checkOwner(club, owner);
        const record = activityId ? getActivity(id, activityId) : club;
        if (!record) throw new ClubError('NOT_FOUND', '活动不存在');
        if (action === 'restore') {
          if (activityId && club.archived) throw new ClubError('CONFLICT', '请先恢复所属社团');
          setArchived(activityId ? 'activities' : 'clubs', record, version, false);
          return { restored: true };
        }
        checkVersion(record, version, true);
        if (activityId) return deleteArchives([], [activityId]);
        const activities = db.prepare('SELECT id FROM activities WHERE club_id=?').all(id).map(item => item.id);
        return deleteArchives([id], activities);
      });
    },

    purgedActivities(ids) {
      return db.prepare(`SELECT id FROM club_archive_tombstones
        WHERE kind='activity' AND id IN (SELECT value FROM json_each(?))`).all(JSON.stringify(ids)).map(row => row.id);
    },

    reviewQueue(offset) {
      const rows = db.prepare("SELECT * FROM clubs ORDER BY review_status='pending' DESC,created_at DESC,id LIMIT 41 OFFSET ?").all(offset);
      return { clubs: rows.slice(0, 40).map(row => ({ id: row.id, name: row.name, category: row.category,
        description: row.description, registrant: row.registrant, created_at: row.created_at, version: row.version,
        archived: Boolean(row.archived), can_manage: false, activities: [], review_status: row.review_status,
        audience: clubAudience(JSON.parse(row.audience)) })), next_offset: rows.length > 40 ? offset + 40 : null };
    },

    reviewClub(id, version, status) {
      if (!['approved', 'rejected'].includes(status)) throw new ClubError('INVALID_ARGUMENT', '审核结果无效');
      return transaction(() => {
        const club = getClub(id);
        if (!club) throw new ClubError('NOT_FOUND', '社团不存在');
        checkVersion(club, version);
        db.prepare('UPDATE clubs SET review_status=?,version=version+1 WHERE id=?').run(status, id);
        return clubView(getClub(id), null);
      });
    },

    manageClub(data) {
      const { action, actor_id: owner, id, version, effect_id: effectId, activity_id: activityId } = data;
      if (!managedActions.includes(action)) throw new ClubError('INVALID_ARGUMENT', '社团操作无效');
      return transaction(() => {
        const club = getClub(id);
        const before = effectId && (activityId ? getActivity(id, activityId) : club);
        if (effectId) {
          const change = db.prepare('SELECT * FROM club_changes WHERE id=?').get(effectId);
          if (change) {
            if (change.actor_id !== owner || change.command !== JSON.stringify(data) || change.undone) {
              throw new ClubError('CONFLICT', '这次社团操作已变化');
            }
            checkOwner(club, owner);
            return clubView(club, owner);
          }
        }
        if (action === 'register') {
          if (!club && wasPurged('club', id)) throw new ClubError('CONFLICT', '原社团已永久删除');
          const fields = clubFields(data);
          const audience = JSON.stringify(clubAudience(data.audience));
          if (club) {
            checkOwner(club, owner);
            if (club.archived || JSON.stringify(clubAudience(JSON.parse(club.audience))) !== audience ||
                [club.name, club.category, club.description].some((value, i) => value !== fields[i])) {
              throw new ClubError('CONFLICT', '登记请求已用于其他资料');
            }
          } else {
            db.prepare('INSERT INTO clubs(id,owner_id,registrant,name,category,description,created_at,audience) VALUES(?,?,?,?,?,?,?,?)')
              .run(id, owner, requiredText(data.registrant, 'registrant'), ...fields, new Date().toISOString(), audience);
          }
        } else {
          checkOwner(club, owner);
          if (action === 'update') {
            checkVersion(club, version);
            const fields = clubFields(data);
            const changed = [club.name, club.category, club.description].some((value, i) => value !== fields[i]);
            const status = changed || club.review_status === 'rejected' ? 'pending' : club.review_status;
            const audience = data.audience === undefined ? club.audience : JSON.stringify(clubAudience(data.audience));
            db.prepare('UPDATE clubs SET name=?,category=?,description=?,review_status=?,audience=?,version=version+1 WHERE id=?')
              .run(...fields, status, audience, id);
          } else if (action === 'archive' || action === 'restore') {
            setArchived('clubs', club, version, action === 'archive');
          } else if (['publish_activity', 'update_activity', 'archive_activity', 'restore_activity'].includes(action)) {
            if (club.archived) throw new ClubError('CONFLICT', '社团已归档');
            const activity = db.prepare(`${activityQuery} WHERE a.id=? AND a.club_id=?`).get(data.activity_id, id);
            if (action === 'publish_activity') {
              if (club.review_status !== 'approved') throw new ClubError('CLUB_REVIEW_REQUIRED', '社团通过审核后才能发布活动');
              const fields = activityFields(data);
              if (activity) {
                if (activity.archived || [activity.name, activity.description, activity.location, activity.starts_at, activity.ends_at, activity.capacity]
                  .some((value, i) => value !== fields[i])) throw new ClubError('CONFLICT', '发布请求已用于其他活动资料');
              } else {
                if (wasPurged('activity', activityId)) throw new ClubError('CONFLICT', '原活动已永久删除');
                db.prepare('INSERT INTO activities(id,club_id,name,description,location,starts_at,ends_at,capacity) VALUES(?,?,?,?,?,?,?,?)')
                  .run(data.activity_id, id, ...fields);
              }
            } else {
              if (!activity) throw new ClubError('NOT_FOUND', '活动不存在');
              if (action === 'update_activity') {
                checkVersion(activity, version, Boolean(activity.archived));
                const fields = activityFields(data);
                if (data.capacity < activity.capacity - activity.remaining) throw new ClubError('CONFLICT', '容量不能小于已报名人数');
                db.prepare('UPDATE activities SET name=?,description=?,location=?,starts_at=?,ends_at=?,capacity=?,version=version+1 WHERE id=?')
                  .run(...fields, activity.id);
              } else {
                setArchived('activities', activity, version, action === 'archive_activity');
              }
            }
          } else {
            throw new ClubError('INVALID_ARGUMENT', '社团操作无效');
          }
        }
        const updated = getClub(id);
        if (effectId) {
          const after = activityId ? getActivity(id, activityId) : updated;
          db.prepare(`INSERT INTO club_changes(id,actor_id,club_id,activity_id,command,before_snapshot,after_version,changed)
            VALUES(?,?,?,?,?,?,?,?)`).run(effectId, owner, id, activityId ?? null, JSON.stringify(data),
            JSON.stringify(before ?? null), after.version, Number(!before || before.version !== after.version));
        }
        return clubView(updated, owner);
      });
    },

    changeStatus(id, owner) {
      const change = getChange(id, owner);
      return change.undone ? 'undone' : change.changed ? 'applied' : 'no_effect';
    },

    undoChange(id, owner) {
      return transaction(() => {
        const change = getChange(id, owner);
        if (change.undone || !change.changed) return change.undone ? 'undone' : 'no_effect';
        if (!change.command) throw new ClubError('CONFLICT', '社团或活动已永久删除');
        const club = getClub(change.club_id);
        checkOwner(club, owner);
        const activityId = change.activity_id;
        const current = activityId ? getActivity(change.club_id, activityId) : club;
        if (!current || current.version !== change.after_version || (activityId && club.archived)) {
          throw new ClubError('CONFLICT', '社团或活动已发生后续变动');
        }
        const before = JSON.parse(change.before_snapshot);
        if (activityId) {
          const registered = db.prepare('SELECT count(*) AS count FROM registrations WHERE activity_id=?').get(activityId).count;
          if (registered > (before?.capacity ?? 0)) throw new ClubError('CONFLICT', '已有报名记录，暂时无法恢复');
          if (before) {
            db.prepare(`UPDATE activities SET name=?,description=?,location=?,starts_at=?,ends_at=?,capacity=?,archived=?,version=version+1
              WHERE id=?`).run(before.name, before.description, before.location, before.starts_at, before.ends_at ?? null,
              before.capacity, before.archived, activityId);
          } else db.prepare('DELETE FROM activities WHERE id=?').run(activityId);
        } else if (before) {
          db.prepare('UPDATE clubs SET name=?,category=?,description=?,archived=?,review_status=?,audience=?,version=version+1 WHERE id=?')
            .run(before.name, before.category, before.description, before.archived, before.review_status ?? 'pending',
              before.audience ?? '{}', change.club_id);
        } else {
          if (db.prepare('SELECT 1 FROM activities WHERE club_id=? LIMIT 1').get(change.club_id)) {
            throw new ClubError('CONFLICT', '社团已有后续活动');
          }
          db.prepare('DELETE FROM clubs WHERE id=?').run(change.club_id);
        }
        db.prepare('UPDATE club_changes SET undone=1 WHERE id=?').run(id);
        if (before) {
          // The previous effect is now current again, but the public version must keep increasing.
          db.prepare(`UPDATE club_changes SET after_version=? WHERE club_id=? AND activity_id IS ?
            AND after_version=? AND changed=1 AND undone=0`).run(current.version + 1, change.club_id, activityId, before.version);
        }
        return 'undone';
      });
    },
  };
}
