const operationPath = '/v1/minta/operations';
const clubPath = '/v1/minta/clubs';
const activitiesPath = '/v1/minta/activities';
const simulation = '当前使用 MINTA 模拟服务。';
const liveStates = new Set(['pending', 'uncertain', 'active', 'closing', 'close_uncertain']);
const registrationLabels = {
  active: '已报名', pending: '报名处理中', uncertain: '报名待确认',
  closing: '取消处理中', close_uncertain: '取消待确认',
};
const activityTimePattern = /^(\d{4}-\d{2}-\d{2})(?:[T ]((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d(?:\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?)?$/;
const beijingOffset = 8 * 3_600_000;
const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' });
const libraryDateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
});
const gymClock = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const loanDay = 24 * 60 * 60 * 1000;
const defaultLoanDays = 7;

const services = {
  classroom: { title: '教室预约', path: '/v1/minta/catalog/classroom', start: '预约', end: '取消预约' },
  gym: { title: '体育馆预约', path: '/v1/minta/catalog/gym', start: '预约', end: '取消预约' },
  club: { title: '校园活动', start: '报名', end: '取消报名' },
  library: { title: '图书馆', path: '/v1/minta/catalog/library', start: '借阅', end: '归还' },
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(label, run) {
  const control = node('button', label, 'outline-button');
  control.type = 'button';
  control.addEventListener('click', () => run(control));
  return control;
}

function list(payload) {
  if (!Array.isArray(payload?.items)) throw new Error('校园服务返回了无法读取的结果。');
  return payload.items;
}

function date(value, format = dateFormat) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : format.format(parsed);
}

function activityTime(value) {
  const match = typeof value === 'string' && activityTimePattern.exec(value);
  if (match) {
    const [, day, time = '', offset] = match;
    const midnight = new Date(`${day}T00:00:00Z`);
    if (Number.isFinite(midnight.getTime()) && midnight.toISOString().slice(0, 10) === day) {
      if (!offset) return { date: day, start_time: time };
      const timestamp = Date.parse(value.replace(' ', 'T'));
      if (Number.isFinite(timestamp)) {
        const beijing = new Date(timestamp + beijingOffset).toISOString();
        return { date: beijing.slice(0, 10), start_time: beijing.slice(11, 16) };
      }
    }
  }
  return { date: '', start_time: '' };
}

export function scheduledActivity(activity) {
  const start = activityTime(activity.starts_at);
  const end = activityTime(activity.ends_at);
  const hasEnd = Boolean(start.start_time && end.start_time &&
    `${end.date}T${end.start_time}` > `${start.date}T${start.start_time}`);
  return { ...activity, ...start, end_date: hasEnd ? end.date : '', end_time: hasEnd ? end.start_time : '' };
}

function compareActivityTime(first, second) {
  const day = (first.date || '9999-99-99').localeCompare(second.date || '9999-99-99');
  return day || (first.start_time || '24:00').localeCompare(second.start_time || '24:00');
}

function activityDateHeading(day) {
  const [year, month, date] = day.split('-').map(Number);
  return `${year}年${month}月${date}日`;
}

export function campusActivityStatus(registration) {
  return registrationLabels[registration?.state] || '';
}

export function campusPointsLabel(points) {
  return Number.isInteger(points) && points >= 1 && points <= 4 ? `${points} 分` : '';
}

export function campusActivityRange(activity, showDate = false) {
  const { date: day, start_time: start, end_date: endDay, end_time: end } = activity;
  if (!day || !start || !end) return '待补全起止时间';
  const prefix = showDate ? `${activityDateHeading(day)} ` : '';
  const endDate = endDay === day ? '' : `${activityDateHeading(endDay)} `;
  return `${prefix}${start}–${endDate}${end}`;
}

function requestKey() {
  // getRandomValues also works when the website is opened over HTTP on the LAN.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}

function submitWithKey(ui, path, label) {
  let key;
  let previous;
  return async (data, control) => {
    const body = JSON.stringify(data);
    if (!key || body !== previous) key = requestKey();
    previous = body;
    const result = await ui.write(path, { method: 'POST', data, key }, control, label);
    if (result?.state === 'failed') key = undefined;
    return result;
  };
}

function form(fields, values, label, submit) {
  const editor = node('form', undefined, 'campus-operation-form');
  for (const [name, title, type, limit, config = {}] of fields) {
    const field = node('label', title);
    if (config.choices) {
      const select = node('select');
      select.name = name;
      select.multiple = true;
      select.size = 5;
      for (const [value, text] of Object.entries(config.choices)) {
        select.append(new Option(text, value, Boolean(values[name]?.includes(value)), Boolean(values[name]?.includes(value))));
      }
      field.append(select);
      editor.append(field);
      continue;
    }
    const input = node(type === 'textarea' ? 'textarea' : 'input');
    if (type !== 'textarea') input.type = type;
    if (type === 'number') {
      input.min = '1';
      input.max = String(limit);
      input.step = '1';
    } else input.maxLength = limit;
    input.name = name;
    input.required = config.required ?? true;
    input.autocomplete = 'off';
    input.defaultValue = values[name] ?? '';
    field.append(input);
    editor.append(field);
  }
  const send = node('button', label, 'primary-button');
  send.type = 'submit';
  editor.append(send);
  editor.addEventListener('submit', event => {
    event.preventDefault();
    const submitted = new FormData(editor);
    const data = Object.fromEntries(submitted);
    for (const [name, , type, , config = {}] of fields) {
      data[name] = config.choices ? submitted.getAll(name) : type === 'number' ? Number(data[name]) : data[name].trim();
    }
    void submit(data, send);
  });
  return editor;
}

function editButton(label, editor) {
  editor.hidden = true;
  const toggle = button(label, control => {
    editor.hidden = !editor.hidden;
    control.setAttribute('aria-expanded', String(!editor.hidden));
    if (!editor.hidden) editor.querySelector('input')?.focus();
    else editor.reset();
  });
  toggle.setAttribute('aria-expanded', 'false');
  return toggle;
}

function row(title, detail) {
  const item = node('li');
  item.append(node('strong', title), node('p', detail));
  return item;
}

function actions(item, ...buttons) {
  const group = node('div', undefined, 'campus-operation-actions');
  group.append(...buttons);
  item.append(group);
}

function section(title, rows, empty) {
  const section = node('section', undefined, 'campus-operation-section');
  section.append(node('h3', title));
  if (!rows.length) section.append(node('p', empty, 'dialog-intro'));
  else {
    const items = node('ul', undefined, 'panel-list');
    items.append(...rows);
    section.append(items);
  }
  return section;
}

function showOperation(item, record, config, ui, focusKey = record.id) {
  if (!['active', 'close_uncertain'].includes(record.state)) {
    item.append(node('p', '操作结果待确认，请刷新查看。'));
    return;
  }
  if (record.state === 'close_uncertain') item.append(node('p', `${config.end}结果待确认，可以重试。`));
  const finish = button(config.end, control => ui.write(
    `${operationPath}/${encodeURIComponent(record.id)}`, { method: 'DELETE' }, control, `${config.end}成功。`,
  ));
  finish.dataset.focusKey = focusKey;
  actions(item, finish);
}

function openOperations(records) {
  return new Map(records.filter(record => liveStates.has(record.state)).map(record => [record.resource_id, record]));
}

function takeOperation(records, id) {
  const record = records.get(id);
  records.delete(id);
  return record;
}

function unlistedOperations(records, config, ui) {
  return Array.from(records.values(), record => {
    const item = row(record.resource_name || record.resource_id, '该项目未列在当前目录。');
    showOperation(item, record, config, ui);
    return item;
  });
}

function activityRow(resource, record, ui, { timeLabel, canStart }) {
  const config = services.club;
  const id = resource.id;
  const detail = [timeLabel, resource.location, campusPointsLabel(resource.points),
    `剩余 ${resource.remaining} / ${resource.capacity} 个名额`, campusActivityStatus(record)]
    .filter(Boolean).join(' · ');

  const item = row(resource.name ?? resource.title, detail);
  if (resource.description) item.append(node('p', resource.description));
  if (record) showOperation(item, record, config, ui, id);
  else if (canStart && resource.remaining > 0 && !resource.archived) {
    const submit = submitWithKey(ui, operationPath, `${config.start}成功。`);
    const start = button(config.start, control => submit({ service: 'club', resource_id: id }, control));
    start.dataset.focusKey = id;
    actions(item, start);
  }
  return item;
}

function serviceDirectory(request) {
  return request.catch(error => {
    if (error.code !== 'campus_access_denied') throw error;
    return { items: [], restricted: true };
  });
}

export async function loadCampusActivities(request, signal) {
  const [directory, registrations] = await Promise.allSettled([
    serviceDirectory(request(activitiesPath, { signal })).then(data => ({ items: list(data), restricted: data.restricted === true })),
    request(`${operationPath}?service=club`, { signal }).then(list),
  ]);
  const signInError = [directory, registrations].find(result => result.reason?.status === 401);
  if (signInError) throw signInError.reason;
  if (directory.status === 'rejected' && registrations.status === 'rejected') throw directory.reason;

  const personal = openOperations(registrations.value || []);
  const activities = (directory.value?.items || []).map(scheduledActivity).filter(activity => activity.end_time)
    .map(activity => ({ ...activity, registration: takeOperation(personal, activity.id) })).sort(compareActivityTime);
  return {
    activities, unlisted: [...personal.values()], restricted: directory.value?.restricted === true,
    activityError: directory.status === 'rejected', registrationError: registrations.status === 'rejected',
  };
}

export function renderCampusActivity(activity, ui, { showDate = true, registrationError = false } = {}) {
  const timeLabel = campusActivityRange(activity, showDate);
  return activityRow(activity, activity.registration, ui, {
    timeLabel, canStart: !registrationError && Boolean(activity.end_time),
  });
}

const activitiesPanel = {
  title: services.club.title,
  intro: `${simulation} 活动时间为北京时间。`,
  poll: 15_000,
  campusActivities: true,
  load: (request, _options, signal) => loadCampusActivities(request, signal),
  render({ activities, unlisted, restricted, activityError, registrationError }, ui, { activityId }) {
    const content = document.createDocumentFragment();
    if (registrationError) content.append(node('p', '暂时无法同步报名状态。', 'dialog-intro'));
    if (restricted) content.append(node('p', '当前校园身份不在开放范围内。你仍可取消已有报名。', 'dialog-intro'));
    else if (activityError) content.append(node('p', '暂时无法读取校园活动。', 'dialog-intro'));
    else if (!activities.length) content.append(node('p', '暂无校园活动。', 'dialog-intro'));
    if (activityId && !restricted && !activityError && !activities.some(activity => activity.id === activityId)) {
      content.append(node('p', '该活动当前已下架或暂不对你的账号开放。', 'dialog-intro'));
    }
    const dates = new Map();
    for (const activity of activities) {
      if (!dates.has(activity.date)) dates.set(activity.date, []);
      const item = renderCampusActivity(activity, ui, { showDate: false, registrationError });
      if (activity.id === activityId) {
        item.classList.add('campus-activity-target');
        item.tabIndex = -1;
        item.dataset.focusKey = `campus-activity-${activity.id}`;
      }
      dates.get(activity.date).push(item);
    }
    for (const [day, rows] of dates) {
      content.append(section(activityDateHeading(day), rows));
    }
    if (unlisted.length) {
      const personal = openOperations(unlisted);
      content.append(section('我的其他报名', unlistedOperations(personal, services.club, ui)));
    }
    return content;
  },
  update(content, options) {
    if (!options.activityId || options.activityLocated) return;
    const target = content.querySelector('.campus-activity-target');
    if (target) {
      options.activityLocated = true;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'nearest' });
    }
  },
};

const classroomDayLimit = 24 * 60;

function classroomToday() {
  return new Date(Date.now() + beijingOffset).toISOString().slice(0, 10);
}

function classroomControls(container, options, reload) {
  const toolbar = node('div', undefined, 'classroom-date-controls');
  const field = node('label', '查看日期');
  const input = node('input');
  input.type = 'date';
  input.min = '0001-01-02';
  input.max = '9999-12-30';
  input.value = options.classroomDate = classroomToday();
  field.append(input);

  function selectDay(day) {
    input.value = day;
    if (!input.value || !input.validity.valid) {
      input.value = options.classroomDate;
      return;
    }
    if (day !== options.classroomDate) {
      options.classroomDate = day;
      reload();
    }
  }

  function shiftDay(days) {
    const day = new Date(`${options.classroomDate}T00:00:00Z`);
    day.setUTCDate(day.getUTCDate() + days);
    selectDay(day.toISOString().slice(0, 10));
  }

  input.addEventListener('change', () => selectDay(input.value));
  const navigation = node('div', undefined, 'classroom-date-navigation');
  navigation.append(button('前一天', () => shiftDay(-1)), button('今天', () => selectDay(classroomToday())),
    button('后一天', () => shiftDay(1)));
  toolbar.append(field, navigation);
  container.append(toolbar);
}

function classroomClock(minute) {
  const hours = String(Math.floor(minute / 60)).padStart(2, '0');
  return `${hours}:${String(minute % 60).padStart(2, '0')}`;
}

function classroomTimeline(slots) {
  const timeline = node('div', undefined, 'classroom-timeline');
  if (!Array.isArray(slots) || slots.some(slot => {
    const { start_minute: start, end_minute: end } = slot ?? {};
    return !Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > classroomDayLimit || start >= end;
  })) {
    timeline.append(node('p', '暂时无法读取占用时段，请刷新后查看。', 'classroom-timeline-message'));
    return timeline;
  }

  const track = node('div', undefined, 'classroom-timeline-track');
  const scale = node('div', undefined, 'classroom-timeline-scale');
  track.setAttribute('aria-hidden', 'true');
  scale.setAttribute('aria-hidden', 'true');
  scale.append(...[0, 360, 720, 1080, classroomDayLimit].map(minute => node('span', classroomClock(minute))));
  timeline.append(track, scale);
  if (!slots.length) {
    timeline.append(node('p', '当天暂无预约记录。', 'classroom-timeline-message'));
    return timeline;
  }

  const details = node('div', undefined, 'classroom-timeline-details');
  details.append(node('span', '占用时段'));
  for (const { start_minute: start, end_minute: end } of slots) {
    const time = `${classroomClock(start)}–${classroomClock(end)}`;
    const reservation = node('span', undefined, 'classroom-timeline-block');
    reservation.style.insetInlineStart = `${start / classroomDayLimit * 100}%`;
    reservation.style.inlineSize = `${(end - start) / classroomDayLimit * 100}%`;
    reservation.title = time;
    track.append(reservation);
    details.append(node('span', time, 'classroom-timeline-time'));
  }
  timeline.append(details);
  return timeline;
}

const classroomPanel = {
  title: services.classroom.title,
  intro: `${simulation} 时间轴按北京时间显示所选日期的预约占用。`,
  controlsInline: true,
  controls: classroomControls,
  async load(request, { role, classroomDate }, signal) {
    const date = classroomDate || classroomToday();
    const query = new URLSearchParams({ date });
    const teacherStatus = role === 'teacher'
      ? request('/v1/minta/teacher-credential', { signal }).catch(error => {
        if (signal.aborted || error.status === 401) throw error;
        return { unavailable: true };
      }) : null;
    const [catalog, operations, teacher] = await Promise.all([
      serviceDirectory(request(`${services.classroom.path}?${query}`, { signal })),
      request(`${operationPath}?service=classroom`, { signal }),
      teacherStatus,
    ]);
    return { resources: list(catalog), records: list(operations), restricted: catalog.restricted, credential: teacher?.credential,
      credentialUnavailable: teacher?.unavailable, role, date };
  },
  render({ resources, records, restricted, credential, credentialUnavailable, role, date: day }, ui) {
    const content = document.createDocumentFragment();
    if (restricted) content.append(node('p', '当前校园身份不在开放范围内。你仍可管理已有的个人办理记录。'));
    let note = '学生预约时需填写老师的姓名和电话以核验凭据。';
    if (role === 'teacher') {
      if (credential) note = `教师凭据已核验，有效至 ${date(credential.valid_until)}。`;
      else if (credentialUnavailable) note = '暂时无法核验教师凭据。已有预约仍可查看和取消。';
      else note = '当前账号缺少有效的教师凭据。请联系凭据管理员核验后预约。';
    }
    content.append(node('p', note, 'dialog-intro'));
    const today = day === classroomToday();
    const rows = resources.map(resource => classroomRow(resource, role, credential, ui, today));
    content.append(section(`${activityDateHeading(day)} · 教室占用`, rows, '当前没有可展示的教室。'));
    const rooms = new Map(resources.map(room => [room.id, room.name]));
    const own = records.filter(record => liveStates.has(record.state) || (record.state === 'closed' && !record.starts_at)).map(record => {
      const item = row(record.resource_name || rooms.get(record.resource_id) || record.resource_id,
        record.starts_at ? campusActivityRange(scheduledActivity(record), true) : '旧预约未记录时段');
      if (record.state === 'active' && Date.parse(record.ends_at) <= Date.now()) item.append(node('p', '已结束'));
      if (record.state === 'closed') item.append(node('p', '已关闭'));
      else showOperation(item, record, services.classroom, ui);
      return item;
    });
    if (own.length) content.append(section('我的教室预约', own));
    return content;
  },
};

function classroomRow(resource, role, credential, ui, today) {
  const { id, name, capacity, reserved, booking_blocked: blocked, busy_slots: occupied } = resource;
  const descriptor = [`容量 ${capacity} 人`];
  if (blocked) descriptor.push('暂停预约');
  else if (today) descriptor.push(reserved ? '此刻占用' : '此刻空闲');
  const item = row(name, descriptor.join(' · '));
  item.append(classroomTimeline(occupied));
  if (blocked || (role !== 'student' && !credential)) return item;
  const fields = [
    ['starts_at', '开始时间（北京时间）', 'datetime-local', 16],
    ['ends_at', '结束时间（北京时间）', 'datetime-local', 16],
  ];
  if (role === 'student') fields.push(['name', '老师姓名', 'text', 80], ['phone', '老师电话号码', 'tel', 40]);
  const submit = submitWithKey(ui, operationPath, '预约成功，已加入个人日程。');
  const editor = form(fields, {}, '提交预约', ({ starts_at, ends_at, name, phone }, control) => {
    const finish = editor.elements.ends_at;
    finish.setCustomValidity(ends_at > starts_at ? '' : '结束时间须晚于开始时间。');
    if (!finish.reportValidity()) return;
    const teacher = role === 'student' ? { name, phone } : undefined;
    return submit({ service: 'classroom', resource_id: id, starts_at, ends_at, teacher }, control);
  });
  editor.addEventListener('input', () => editor.elements.ends_at.setCustomValidity(''));
  editor.addEventListener('reset', () => editor.elements.ends_at.setCustomValidity(''));
  const toggle = editButton('预约时段', editor);
  toggle.dataset.focusKey = `classroom-${id}`;
  actions(item, toggle);
  item.append(editor);
  return item;
}

function gymTime(slot) {
  return `${date(slot.starts_at, gymClock)}–${date(slot.ends_at, gymClock)}`;
}

function gymEditor(venue, bookingWindow, identity, request, panelSignal, ui) {
  const editor = node('form', undefined, 'gym-booking');
  const choices = node('div', undefined, 'gym-booking-choices');
  const day = node('input');
  day.type = 'date';
  day.min = day.defaultValue = bookingWindow.first_date;
  day.max = bookingWindow.last_date;
  day.required = true;
  const time = node('select');
  const court = node('select');
  for (const [title, control] of [['日期', day], ['时段（北京时间）', time], ['场地号', court]]) {
    const label = node('label', title);
    label.append(control);
    choices.append(label);
  }
  const status = node('p', '', 'dialog-intro');
  status.setAttribute('role', 'status');
  const preview = node('div', undefined, 'gym-booking-preview');
  preview.hidden = true;
  const summary = node('p');
  const checked = node('input');
  checked.type = 'checkbox';
  const checkLabel = node('label', undefined, 'gym-identity-check');
  checkLabel.append(checked, node('span', `确认以 ${identity} 的身份预约`));
  const confirm = node('button', '确认并提交', 'primary-button');
  confirm.type = 'submit';
  confirm.disabled = true;
  confirm.dataset.focusKey = venue.id;
  preview.append(node('strong', '核对预约'), summary, checkLabel, confirm);
  editor.append(choices, status, preview);
  const submit = submitWithKey(ui, operationPath, '预约成功。');
  let slots = [];
  let pending;

  function selectSlot() {
    const slot = slots.find(item => item.slot_id === court.value && item.remaining > 0);
    checked.checked = false;
    confirm.disabled = true;
    preview.hidden = !slot;
    summary.textContent = slot ? `${venue.name} · ${slot.court_name} · ${day.value} ${gymTime(slot)}` : '';
    return slot;
  }

  function selectTime() {
    court.replaceChildren(new Option('选择场地号', ''));
    const matches = slots.filter(slot => slot.starts_at === time.value);
    court.disabled = !matches.length;
    for (const slot of matches) {
      const option = new Option(`${slot.court_name} · ${slot.remaining > 0 ? `余 ${slot.remaining}` : '已满'}`, slot.slot_id);
      option.disabled = !slot.remaining;
      court.append(option);
    }
    selectSlot();
  }

  async function loadSlots() {
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    slots = [];
    time.replaceChildren(new Option('选择时段', ''));
    time.disabled = true;
    selectTime();
    if (!day.value || !day.validity.valid) {
      status.textContent = '请选择可预约范围内的日期。';
      return;
    }
    status.textContent = '正在读取场地余量…';
    try {
      const query = new URLSearchParams({ venue_id: venue.id, date: day.value });
      const result = await request(`${services.gym.path}?${query}`, {
        signal: AbortSignal.any([controller.signal, panelSignal]),
      });
      if (controller.signal.aborted || panelSignal.aborted || !editor.isConnected) return;
      slots = list(result);
      const periods = new Map();
      for (const slot of slots) {
        const period = periods.get(slot.starts_at) || { label: gymTime(slot), remaining: 0 };
        if (slot.remaining > 0) period.remaining++;
        periods.set(slot.starts_at, period);
      }
      for (const [start, period] of periods) {
        const option = new Option(`${period.label} · ${period.remaining ? `${period.remaining} 个场地可约` : '已满'}`, start);
        option.disabled = !period.remaining;
        time.append(option);
      }
      time.disabled = !slots.some(slot => slot.remaining > 0);
      status.textContent = time.disabled ? '这一天暂无可预约时段。' : '';
    } catch (error) {
      if (!controller.signal.aborted && !panelSignal.aborted) status.textContent = error.message;
    }
  }

  day.addEventListener('change', loadSlots);
  time.addEventListener('change', selectTime);
  court.addEventListener('change', selectSlot);
  checked.addEventListener('change', () => { confirm.disabled = !checked.checked; });
  editor.addEventListener('submit', async event => {
    event.preventDefault();
    if (!checked.checked || !court.value || confirm.disabled) return;
    const result = await submit({ service: 'gym', resource_id: venue.id, slot_id: court.value, identity_confirmed: true }, confirm);
    if (result?.state === 'failed' && editor.isConnected && !editor.hidden) await loadSlots();
  });
  return { editor, loadSlots };
}

const gymPanel = {
  title: services.gym.title,
  intro: '按北京时间选择日期、时段和场地号。当前使用 MINTA 模拟服务。',
  async load(request, { account }, signal) {
    const [catalog, operations] = await Promise.all([
      serviceDirectory(request(services.gym.path, { signal })),
      request(`${operationPath}?service=gym`, { signal }),
    ]);
    const role = account?.role === 'teacher' ? '教师' : '学生';
    const name = account?.full_name || account?.display_name || account?.email;
    return { catalog, operations, request, identity: name ? `${name}（${role}）` : `当前登录的${role}账号` };
  },
  render({ catalog, operations, request, identity }, ui, { panelSignal }) {
    const content = document.createDocumentFragment();
    if (catalog.restricted) content.append(node('p', '当前校园身份不在开放范围内。你仍可取消已有预约。'));
    const records = list(operations).filter(record => liveStates.has(record.state)).map(record => {
      const detail = record.starts_at
        ? `${record.court_name} · ${date(record.starts_at, libraryDateTime)} 至 ${date(record.ends_at, gymClock)}`
        : record.slot_id ? record.slot_id.replaceAll('_', ' · ') : '早期预约记录';
      const item = row(record.resource_name || record.resource_id, detail);
      showOperation(item, record, services.gym, ui);
      return item;
    });
    content.append(section('我的预约', records, '当前没有预约。'));
    const bookingWindow = catalog.booking_window;
    const venues = list(catalog).map(venue => {
      const item = row(venue.name, venue.court_count ? `${venue.court_count} 个场地` : '');
      if (!bookingWindow) return item;
      const { editor, loadSlots } = gymEditor(venue, bookingWindow, identity, request, panelSignal, ui);
      const select = editButton('选择时段', editor);
      select.dataset.focusKey = venue.id;
      select.addEventListener('click', () => { if (!editor.hidden) void loadSlots(); });
      actions(item, select);
      item.append(editor);
      return item;
    });
    if (!catalog.restricted) {
      if (venues.length && !bookingWindow) content.append(node('p', '当前场馆服务尚未提供时段，请稍后刷新。', 'dialog-intro'));
      content.append(section('选择场馆', venues, '暂无开放的场馆。'));
    }
    return content;
  },
};

function loanProgress(loan, label) {
  const period = node('div', undefined, 'library-period');
  period.dataset.borrowedAt = loan.borrowed_at;
  period.dataset.dueAt = loan.due_at;
  const heading = node('div', undefined, 'library-period-line');
  heading.append(node('span', label), node('strong', undefined, 'library-remaining'));
  const bar = node('progress', undefined, 'library-progress');
  bar.max = 100;
  bar.setAttribute('aria-label', `${label}的借期进度`);
  const details = node('div', undefined, 'library-period-line library-period-detail');
  const deadline = node('time', `${date(loan.due_at, libraryDateTime)} 到期`);
  deadline.dateTime = loan.due_at;
  details.append(deadline, node('span', undefined, 'library-elapsed'));
  period.append(heading, bar, details);
  return period;
}

function remainingLoanTime(ms) {
  const minutes = Math.max(1, Math.ceil(ms / 60000));
  const hours = Math.floor(minutes / 60);
  if (hours >= 24) return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes % 60} 分钟`;
  return `${minutes} 分钟`;
}

function updateLoanProgress(content) {
  const now = Date.now();
  for (const period of content.querySelectorAll('.library-period')) {
    const start = Date.parse(period.dataset.borrowedAt);
    const due = Date.parse(period.dataset.dueAt);
    const overdue = now >= due;
    const percent = Math.max(0, Math.min(overdue ? 100 : 99, Math.floor((now - start) * 100 / (due - start))));
    const remaining = overdue ? '已逾期' : `剩余 ${remainingLoanTime(due - now)}`;
    const elapsed = `借期已用 ${percent}%`;
    const bar = period.querySelector('progress');
    bar.value = percent;
    bar.setAttribute('aria-valuetext', `${elapsed}，${remaining}`);
    period.dataset.overdue = String(overdue);
    period.querySelector('.library-remaining').textContent = remaining;
    period.querySelector('.library-elapsed').textContent = elapsed;
    const finish = period.parentElement.querySelector('[data-library-return]');
    if (finish) finish.textContent = overdue ? '归还' : '提前归还';
  }
  for (const estimate of content.querySelectorAll('[data-loan-estimate]')) {
    estimate.textContent = `预计 ${date(now + Number(estimate.dataset.loanEstimate) * loanDay, libraryDateTime)} 到期`;
  }
}

function libraryCheckout(book, draft) {
  const editor = node('form', undefined, 'library-checkout');
  const choices = node('fieldset', undefined, 'library-duration');
  choices.append(node('legend', '借阅时长'));
  const grid = node('div', undefined, 'library-duration-grid');
  const estimate = node('p', undefined, 'library-estimate');
  const updateEstimate = () => {
    estimate.dataset.loanEstimate = draft.days;
    estimate.textContent = `预计 ${date(Date.now() + draft.days * loanDay, libraryDateTime)} 到期`;
  };
  for (let days = 1; days <= 14; days++) {
    const label = node('label');
    const radio = node('input', undefined, 'sr-only');
    radio.type = 'radio';
    radio.name = 'duration_days';
    radio.value = String(days);
    radio.checked = days === draft.days;
    radio.dataset.focusKey = `${book.id}-loan-${days}`;
    radio.addEventListener('change', () => {
      draft.days = days;
      updateEstimate();
    });
    label.append(radio, node('span', `${days} 天`));
    grid.append(label);
  }
  choices.append(grid);
  updateEstimate();
  const footer = node('div', undefined, 'library-checkout-footer');
  const submit = node('button', '确认借阅', 'primary-button');
  submit.type = 'submit';
  submit.dataset.focusKey = `${book.id}-loan-submit`;
  footer.append(estimate, submit);
  editor.append(choices, footer);
  editor.addEventListener('submit', event => {
    event.preventDefault();
    void draft.submit({ service: 'library', resource_id: book.id, duration_days: draft.days }, submit);
  });
  return editor;
}

function libraryBookCard(book, owned, state, selectBook) {
  const item = node('li');
  const card = node('button', undefined, 'library-card');
  card.type = 'button';
  card.dataset.bookId = book.id;
  card.dataset.focusKey = `library-book-${book.id}`;
  card.dataset.category = book.category_id;
  card.title = `${book.title} · ${book.author}`;
  card.setAttribute('aria-pressed', String(state.selectedId === book.id));
  card.setAttribute('aria-controls', 'library-book-selection');
  card.addEventListener('click', () => selectBook(book.id));
  const cover = node('span', undefined, 'library-card-cover');
  cover.append(node('span', book.category, 'library-category'), node('strong', book.title, 'library-card-title'));
  const details = node('span', undefined, 'library-card-details');
  const stock = book.available_copies ? `可借 ${book.available_copies} 册` : '暂无可借';
  details.append(node('span', book.author, 'library-card-author'), node('span', stock, 'library-card-stock'));
  if (owned) details.append(node('span', '我的借阅'));
  else if (book.borrowed_copies) details.append(node('span', `借出 ${book.borrowed_copies} 册`));
  card.append(cover, details);
  item.append(card);
  return item;
}

function libraryShelf(catalog, books, personal, state, ui, selectBook) {
  const shelf = node('section', undefined, 'library-shelf');
  shelf.setAttribute('aria-labelledby', 'library-shelf-heading');
  const header = node('div', undefined, 'library-shelf-head');
  const title = node('h3', '馆藏书架');
  title.id = 'library-shelf-heading';
  const { summary, categories, total, page, page_size: pageSize } = catalog;
  header.append(title, node('p', `${summary.titles} 种 · 可借 ${summary.available_copies} 册`));

  const filters = node('form', undefined, 'library-filters');
  const search = node('input');
  search.type = 'search';
  search.maxLength = 120;
  search.value = state.input;
  search.placeholder = '搜索书名、作者或馆藏编号';
  search.setAttribute('aria-label', '搜索书名、作者或馆藏编号');
  search.dataset.focusKey = 'library-search';
  const category = node('select');
  category.setAttribute('aria-label', '馆藏分类');
  category.dataset.focusKey = 'library-category';
  category.append(new Option('全部分类', ''));
  for (const entry of categories) category.append(new Option(`${entry.label}（${entry.titles}）`, entry.id));
  category.value = state.category;

  const apply = () => {
    clearTimeout(state.searchTimer);
    const query = state.input.trim();
    if (state.q === query && state.category === category.value && state.page === 1) return;
    state.q = query;
    state.category = category.value;
    state.page = 1;
    void ui.reload();
  };
  const scheduleSearch = () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(apply, 260);
  };
  search.addEventListener('input', event => {
    state.input = search.value;
    if (!event.isComposing) scheduleSearch();
  });
  search.addEventListener('compositionstart', () => clearTimeout(state.searchTimer));
  search.addEventListener('compositionend', () => {
    state.input = search.value;
    scheduleSearch();
  });
  category.addEventListener('change', apply);
  filters.addEventListener('submit', event => {
    event.preventDefault();
    apply();
  });
  const submit = node('button', '搜索', 'outline-button');
  submit.type = 'submit';
  filters.append(search, category, submit);
  shelf.append(header, filters);

  if (books.length) {
    const grid = node('ul', undefined, 'library-shelf-grid');
    grid.append(...books.map(book => libraryBookCard(book, personal.get(book.id), state, selectBook)));
    shelf.append(grid);
  } else {
    const empty = node('div', undefined, 'library-shelf-empty');
    empty.append(node('p', '没有符合条件的馆藏。'));
    if (state.q || state.category) empty.append(button('清除筛选', () => {
      search.value = state.input = category.value = '';
      search.focus();
      apply();
    }));
    shelf.append(empty);
  }
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  const footer = node('div', undefined, 'library-shelf-foot');
  const result = node('p', total ? `${total} 种结果 · 第 ${page} / ${lastPage} 页` : '0 种结果');
  result.setAttribute('role', 'status');
  footer.append(result);
  if (lastPage > 1) {
    const pagination = node('div', undefined, 'library-pagination');
    for (const [label, target] of [['上一页', page - 1], ['下一页', page + 1]]) {
      const step = button(label, () => {
        state.page = target;
        void ui.reload();
      });
      step.disabled = target < 1 || target > lastPage;
      step.dataset.focusKey = `library-page-${label}`;
      pagination.append(step);
    }
    footer.append(pagination);
  }
  shelf.append(footer);
  return shelf;
}

function librarySelection(book, owned, state, ui) {
  const detail = document.createDocumentFragment();
  if (!book) {
    detail.append(node('p', '从上方书架选择一本书，即可查看库存和办理借阅。', 'library-selection-empty'));
    return detail;
  }
  detail.append(node('h4', book.title), node('p', `${book.author} · ${book.category} · ${book.id}`, 'library-book-meta'));
  detail.append(node('p', `可借 ${book.available_copies} / ${book.total_copies} 册 · 借出 ${book.borrowed_copies} 册`, 'library-book-stock'));
  if (owned) {
    const note = owned.state === 'active' ? '你已借阅此书，可在「我的借阅」归还。' : '你的借还操作正在处理。';
    detail.append(node('p', note, 'library-owned-note'));
  } else if (book.available_copies > 0) {
    if (!state.drafts.has(book.id)) state.drafts.set(book.id, {
      days: defaultLoanDays, submit: submitWithKey(ui, operationPath, '借阅成功。'),
    });
    detail.append(libraryCheckout(book, state.drafts.get(book.id)));
  } else {
    detail.append(node('p', '当前所有副本均已借出。', 'library-book-meta'));
  }
  if (book.borrowed_copies) {
    const periods = node('div', undefined, 'library-book-periods');
    periods.append(node('h5', '匿名借阅进度'));
    book.loan_progress.forEach((loan, index) => periods.append(loanProgress(loan, `借出副本 ${index + 1}`)));
    const unknown = book.borrowed_copies - book.loan_progress.length;
    if (unknown > 0) periods.append(node('p', `${unknown} 册借期未登记。`, 'library-book-meta'));
    detail.append(periods);
  }
  return detail;
}

function myLibraryLoan(record, ui) {
  const item = node('li', undefined, 'library-book');
  item.append(node('strong', record.resource_name || record.resource_id));
  if (record.borrowed_at && record.due_at) {
    item.append(loanProgress(record, `借期 ${record.duration_days} 天`));
  }
  if (record.state === 'active' || record.state === 'close_uncertain') {
    const retry = record.state === 'close_uncertain';
    if (retry) item.append(node('p', '归还结果待确认，可以重试。'));
    const finish = button(retry ? '重试归还' : '归还', control => ui.write(
      `${operationPath}/${encodeURIComponent(record.id)}`, { method: 'DELETE' }, control, '图书已归还。',
    ));
    if (!retry) finish.dataset.libraryReturn = '';
    finish.dataset.focusKey = record.id;
    actions(item, finish);
  } else item.append(node('p', '操作结果待确认，请刷新查看。'));
  return item;
}

function libraryLoans(records, personal, state, ui) {
  const loans = section('我的借阅', Array.from(personal.values(), record => myLibraryLoan(record, ui)), '当前没有未归还的图书。');
  loans.classList.add('library-loans');
  const closed = records.filter(record => record.state === 'closed');
  if (closed.length) {
    const history = node('details', undefined, 'library-history');
    history.open = state.historyOpen;
    history.addEventListener('toggle', () => { if (history.isConnected) state.historyOpen = history.open; });
    history.append(node('summary', `已归还 · ${closed.length}`));
    const items = node('ul', undefined, 'panel-list');
    items.append(...closed.map(record => row(record.resource_name || record.resource_id,
      record.borrowed_at ? `${date(record.borrowed_at, libraryDateTime)} 借出 · ${record.duration_days} 天` : '已归还')));
    history.append(items);
    loans.append(history);
  }
  return loans;
}

const libraryPanel = {
  title: services.library.title,
  intro: 'MINTA 模拟馆藏。库存和借阅进度自动刷新，到期时间按北京时间显示。',
  poll: 5000,
  async load(request, options, signal) {
    const state = options.library ??= { input: '', q: '', category: '', page: 1, selectedId: '', drafts: new Map() };
    const params = new URLSearchParams({ page: state.page });
    if (state.q) params.set('q', state.q);
    if (state.category) params.set('category', state.category);
    const [catalog, operations] = await Promise.all([
      serviceDirectory(request(`${services.library.path}?${params}`, { signal })),
      request(`${operationPath}?service=library`, { signal }),
    ]);
    return { catalog, books: list(catalog), records: list(operations) };
  },
  render({ catalog, books, records }, ui, { library: state }) {
    const content = document.createDocumentFragment();
    const personal = openOperations(records);
    for (const id of personal.keys()) state.drafts.delete(id);
    const loans = libraryLoans(records, personal, state, ui);
    if (catalog.restricted) {
      content.append(node('p', '当前校园身份不在馆藏开放范围内。你仍可归还已有借阅。'), loans);
      return content;
    }
    state.page = catalog.page;
    if (!books.some(book => book.id === state.selectedId)) state.selectedId = '';
    const desk = node('div', undefined, 'library-desk');
    const operation = node('section', undefined, 'library-operation');
    const heading = node('h3', '借阅操作');
    heading.tabIndex = -1;
    heading.id = 'library-operation-heading';
    heading.dataset.focusKey = 'library-operation-heading';
    operation.setAttribute('aria-labelledby', heading.id);
    const selected = node('div', undefined, 'library-selection');
    selected.id = 'library-book-selection';
    const drawSelection = () => {
      const book = books.find(item => item.id === state.selectedId);
      selected.replaceChildren(librarySelection(book, personal.get(state.selectedId), state, ui));
      updateLoanProgress(selected);
    };
    const shelf = libraryShelf(catalog, books, personal, state, ui, id => {
      state.selectedId = id;
      for (const card of shelf.querySelectorAll('[data-book-id]')) {
        card.setAttribute('aria-pressed', String(card.dataset.bookId === id));
      }
      drawSelection();
      heading.focus({ preventScroll: true });
      heading.scrollIntoView({ block: 'start' });
    });
    drawSelection();
    operation.append(heading, selected);
    desk.append(operation, loans);
    content.append(shelf, desk);
    return content;
  },
  update: updateLoanProgress,
};

const clubFields = [
  ['name', '社团名称', 'text', 80], ['category', '类别', 'text', 40],
  ['description', '社团介绍', 'textarea', 1000],
];
const activityFields = [
  ['name', '活动名称', 'text', 100], ['description', '活动介绍', 'textarea', 1000],
  ['location', '地点', 'text', 120], ['starts_at', '开始时间（北京时间）', 'datetime-local', 80],
  ['ends_at', '结束时间（北京时间）', 'datetime-local', 80],
  ['capacity', '报名名额', 'number', 10000],
];

function clubEditor(ui, fields, initial, data, label, grades) {
  const message = data.action === 'register' ? '登记已提交，审核通过后进入公开目录。' : `${label}成功。`;
  const submit = submitWithKey(ui, clubPath, message);
  if (fields === activityFields) {
    const toLocal = value => {
      const time = activityTime(value);
      return time.start_time ? `${time.date}T${time.start_time}` : '';
    };
    const start = toLocal(initial.starts_at);
    const end = toLocal(initial.ends_at);
    const draft = { ...initial, starts_at: start, ends_at: end };
    const editor = form(fields, draft, label, (values, control) => {
      if (values.ends_at <= values.starts_at) {
        const input = editor.elements.ends_at;
        input.setCustomValidity('结束时间须晚于开始时间。');
        input.reportValidity();
        return;
      }
      if (values.starts_at === start && values.ends_at === end) {
        values.starts_at = initial.starts_at;
        values.ends_at = initial.ends_at;
      }
      return submit({ ...data, ...values }, control);
    });
    editor.addEventListener('input', () => editor.elements.ends_at.setCustomValidity(''));
    editor.addEventListener('reset', () => editor.elements.ends_at.setCustomValidity(''));
    return editor;
  }
  if (!grades) return form(fields, initial, label, (values, control) => submit({ ...data, ...values }, control));
  const options = [...fields,
    ['allow_grades', '开放年级（可多选，留空不限）', 'select', 0, { choices: grades }],
    ['allow_colleges', '开放学院（逗号或换行分隔，留空不限）', 'textarea', 3000, { required: false }],
  ];
  const values = { ...initial, allow_grades: initial.audience?.allow_grades,
    allow_colleges: initial.audience?.allow_colleges?.join('\n') };
  return form(options, values, label, ({ allow_grades, allow_colleges, ...details }, control) => submit({
    ...data, ...details, audience: { allow_grades, allow_colleges: allow_colleges.split(/[,，\n]/).map(item => item.trim()).filter(Boolean) },
  }, control));
}

function archiveButton(ui, data, name, label) {
  const submit = submitWithKey(ui, clubPath, `${label}成功。`);
  return button(label, control => {
    if (window.confirm(`确定归档「${name}」？归档后将从公开列表撤下，可以随时恢复。`)) void submit(data, control);
  });
}

function archiveControls(ui, data, name, activity = false) {
  const restore = submitWithKey(ui, clubPath, '已恢复归档。');
  const purge = submitWithKey(ui, clubPath, '已永久删除归档。');
  const remove = button('永久删除', control => {
    const related = activity ? '相关报名会结束。' : '社团所属活动和相关报名也会删除。';
    if (window.confirm(`确定永久删除「${name}」？${related}删除后无法恢复。`)) {
      void purge({ ...data, action: 'purge_archive' }, control);
    }
  });
  remove.className = 'danger-text';
  return [button('恢复归档', control => void restore({ ...data, action: activity ? 'restore_activity' : 'restore' }, control)), remove];
}

function purgeArchivesButton(ui, { club_count, activity_count, snapshot }) {
  const targets = [
    club_count && `${club_count} 个已归档社团`,
    activity_count && `${activity_count} 个归档范围内的活动`,
  ].filter(Boolean).join('、');
  const submit = submitWithKey(ui, clubPath, '归档已清空。');
  const control = button('清空归档', control => {
    const message = `确定永久删除你名下的 ${targets}？相关报名会结束，删除后无法恢复。`;
    if (window.confirm(message)) void submit({ action: 'purge_archives', snapshot }, control);
  });
  control.className = 'danger-text';
  control.dataset.focusKey = 'club-archive-cleanup';
  return control;
}

function clubActivity(club, activity, record, ui) {
  const item = renderCampusActivity({
    ...activity, archived: club.archived || activity.archived || !club.can_join, registration: record,
  }, ui);
  const details = { id: club.id, activity_id: activity.id, version: activity.version };
  if (activity.archived) item.append(node('p', '已归档'));
  if (!club.can_manage || club.archived) return item;
  if (activity.archived) actions(item, ...archiveControls(ui, details, activity.name, true));
  if (!activity.archived || !activity.end_time) {
    const editor = clubEditor(ui, activityFields, activity, { ...details, action: 'update_activity' }, '保存活动');
    const edit = editButton(activity.end_time ? '编辑活动' : '补全起止时间', editor);
    if (activity.archived) actions(item, edit);
    else actions(item, edit, archiveButton(ui, { ...details, action: 'archive_activity' }, activity.name, '归档活动'));
    item.append(editor);
  }
  return item;
}

function clubRow(club, records, ui, grades) {
  const item = row(club.name, `${club.category} · 登记人 ${club.registrant} · ${date(club.created_at)}`);
  const details = { id: club.id, version: club.version };
  item.append(node('p', club.description));
  if (club.archived) {
    item.append(node('p', '已归档'));
    if (club.can_manage) actions(item, ...archiveControls(ui, details, club.name));
  } else if (club.can_manage) {
    const review = { pending: '待管理员审核', approved: '已通过审核', rejected: '审核未通过，修改后可重新提交' };
    item.append(node('p', review[club.review_status] || ''));
    const editor = clubEditor(ui, clubFields, club, { ...details, action: 'update' }, '保存社团', grades);
    const publish = club.review_status === 'approved'
      ? clubEditor(ui, activityFields, {}, { action: 'publish_activity', id: club.id }, '发布活动') : null;
    actions(item, editButton('编辑资料', editor), ...(publish ? [editButton('发布活动', publish)] : []),
      archiveButton(ui, { ...details, action: 'archive' }, club.name, '归档社团'));
    item.append(editor);
    if (publish) item.append(publish);
  }
  const activities = club.activities.map(scheduledActivity)
    .sort(compareActivityTime).map(activity => clubActivity(club, activity, takeOperation(records, activity.id), ui));
  item.append(section('社团活动', activities, '当前没有已发布活动。'));
  return item;
}

export const operationPanels = [
  ['classrooms', classroomPanel],
  ['gym', gymPanel],
  ['activities', activitiesPanel],
  ['library', libraryPanel],
  ['clubs', {
    title: '社团',
    intro: `${simulation} 社团通过审核后可发布活动，并按负责人指定的学院和年级公开。`,
    campusActivities: true,
    async load(request, options, signal) {
      const [clubs, operations] = await Promise.all([
        serviceDirectory(request(clubPath, { signal })), request(`${operationPath}?service=club`, { signal }),
      ]);
      return { clubs: list(clubs), records: list(operations), grades: clubs.grades, restricted: clubs.restricted,
        archiveSummary: clubs.archive_summary };
    },
    render({ clubs, records, grades, restricted, archiveSummary }, ui) {
      const content = document.createDocumentFragment();
      const personal = openOperations(records);
      const editor = clubEditor(ui, clubFields, {}, { action: 'register' }, '提交登记', grades);
      const owned = [], directory = [];
      for (const club of clubs) (club.can_manage ? owned : directory).push(clubRow(club, personal, ui, grades));
      if (restricted) content.append(node('p', '当前校园身份不在开放范围内。你仍可取消已有报名。'));
      else {
        const managed = section('我登记的社团', owned, '你还没有登记社团。');
        if (archiveSummary && (archiveSummary.club_count > 0 || archiveSummary.activity_count > 0)) {
          managed.firstElementChild.after(purgeArchivesButton(ui, archiveSummary));
        }
        content.append(editButton('登记社团', editor), editor, managed, section('社团目录', directory, '暂无其他已公开的社团。'));
      }
      if (personal.size) content.append(section('我的其他报名', unlistedOperations(personal, services.club, ui)));
      return content;
    },
  }],
];
