import { campusActivityRange, campusActivityStatus, campusPointsLabel, loadCampusActivities, renderCampusActivity, scheduledActivity } from './campus-operations.js';

const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const scales = ['一周', '一天', '小时'];
const lastTeachingWeek = 20;
const clockTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const demoTermStart = Date.UTC(2026, 8, 7);
const dayMs = 86_400_000;
const beijingOffset = 8 * 3_600_000;
const eventsPath = '/v1/calendar/events';
const eventLabels = { classroom: '教室预约', custom: '自定义事件' };

function beijingNow() {
  return new Date(Date.now() + beijingOffset);
}

function weekDates(week) {
  return days.map((_, index) => new Date(demoTermStart + ((week - 1) * 7 + index) * dayMs).toISOString().slice(0, 10));
}

function eventId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

function dayLabel(index, date) {
  return `${days[index]} ${Number(date.slice(5, 7))}/${Number(date.slice(8, 10))}`;
}

function isTeachingWeek(week) {
  return week >= 1 && week <= lastTeachingWeek;
}

function weekLabel(week) {
  if (isTeachingWeek(week)) return `第 ${week} 周`;
  const dates = weekDates(week);
  return `${dates[0]} 至 ${dates[6]}`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, className, activate, label) {
  const node = element('button', className, text);
  node.type = 'button';
  if (label) node.setAttribute('aria-label', label);
  node.addEventListener('click', activate);
  return node;
}

function scheduleControls(controls, state, reload) {
  const now = beijingNow();
  state.week = 1 + Math.floor((now.getTime() - demoTermStart) / (7 * dayMs));
  state.day = (now.getUTCDay() + 6) % 7;
  state.scale = 0;
  const key = state.userId ? `campus-desk.schedule-courses.${state.userId}` : null;
  state.showCourses = true;
  if (key) {
    try { state.showCourses = localStorage.getItem(key) !== 'hidden'; } catch { /* 仍可在本次打开时切换。 */ }
  }

  const panel = element('div', 'schedule-controls');
  const week = element('div', 'schedule-week-control');
  const heading = element('label', 'schedule-control-label', '周次');
  const select = element('select');
  select.id = 'campus-panel-week';
  heading.htmlFor = select.id;
  const previous = button('‹', 'outline-button', () => changeWeek(state.week - 1), '上一周');
  const next = button('›', 'outline-button', () => changeWeek(state.week + 1), '下一周');
  const weekPicker = element('div', 'schedule-week-picker');
  weekPicker.append(previous, select, next);
  week.append(heading, weekPicker);
  fillWeekOptions();

  function fillWeekOptions() {
    const choices = new Set([...Array.from({ length: lastTeachingWeek }, (_, index) => index + 1),
      state.week - 1, state.week, state.week + 1]);
    select.replaceChildren(...[...choices].sort((a, b) => a - b)
      .map(value => new Option(weekLabel(value), String(value), false, value === state.week)));
  }

  function changeWeek(value) {
    if (value === state.week) return;
    state.week = value;
    state.activityId = null;
    state.renderView = null;
    fillWeekOptions();
    reload();
  }
  select.addEventListener('change', () => changeWeek(Number(select.value)));

  const zoom = element('div', 'schedule-zoom');
  const zoomLabel = element('label', 'schedule-control-label', '时间缩放');
  const slider = element('input');
  slider.type = 'range';
  slider.id = 'campus-schedule-zoom';
  slider.min = '0';
  slider.max = String(scales.length - 1);
  slider.step = '1';
  zoomLabel.htmlFor = slider.id;
  const labels = element('div', 'schedule-scale-labels');
  const stops = scales.map((label, index) => button(label, '', () => setScale(index)));
  labels.append(...stops);
  zoom.append(zoomLabel, slider, labels);

  const visibility = element('label', 'schedule-visibility');
  const toggle = element('input');
  toggle.type = 'checkbox';
  toggle.setAttribute('role', 'switch');
  toggle.checked = state.showCourses;
  const track = element('span', 'schedule-switch-track');
  track.setAttribute('aria-hidden', 'true');
  const visibilityLabel = element('span');
  state.setTeachingLabel = teaching => {
    visibilityLabel.textContent = teaching ? '显示授课安排' : '显示课表';
  };
  state.setTeachingLabel(state.role === 'teacher');
  visibility.append(visibilityLabel, toggle, track);
  toggle.addEventListener('change', () => {
    state.showCourses = toggle.checked;
    if (key) {
      try { localStorage.setItem(key, toggle.checked ? 'shown' : 'hidden'); } catch { /* 仍可在本次打开时切换。 */ }
    }
    state.renderView?.();
  });
  panel.append(week, zoom, visibility);
  controls.append(panel);

  function showScale() {
    slider.value = String(state.scale);
    slider.setAttribute('aria-valuetext', scales[state.scale]);
    stops.forEach((stop, index) => stop.setAttribute('aria-pressed', String(index === state.scale)));
  }
  function setScale(value) {
    if (value === state.scale) return;
    const direction = value > state.scale ? 'in' : 'out';
    state.scale = value;
    showScale();
    state.renderView?.(direction);
  }
  state.setScale = setScale;
  slider.addEventListener('input', () => setScale(slider.valueAsNumber));
  showScale();
}

function validRange(start, end) {
  return typeof start === 'string' && typeof end === 'string' && clockTime.test(start) && clockTime.test(end) && start < end;
}

function scheduleData(payload) {
  if (payload?.source !== 'minta_simulation' || !Array.isArray(payload.items)) {
    throw new Error('校务服务返回了无法读取的结果。');
  }
  const periods = new Map();
  for (const { period, start_time: start, end_time: end } of payload.periods ?? []) {
    if (Number.isInteger(period) && period >= 1 && period <= 255 && validRange(start, end)) {
      periods.set(period, [start, end]);
    }
  }
  const columns = days.map(() => []);
  for (const course of payload.items) {
    const day = days.indexOf(course?.day);
    if (day < 0 || !Number.isInteger(course.start_period) || !Number.isInteger(course.end_period) ||
        course.start_period < 1 || course.end_period < course.start_period || course.end_period > 255) {
      throw new Error('日程中的星期或节次无效。');
    }
    const start = clockTime.test(course.start_time) ? course.start_time : periods.get(course.start_period)?.[0];
    const end = clockTime.test(course.end_time) ? course.end_time : periods.get(course.end_period)?.[1];
    columns[day].push({ ...course, start_time: start, end_time: end });
  }
  return columns;
}

function courseCard(course, teaching) {
  const card = element('div', 'schedule-course');
  const { start_period: start, end_period: end, course_name: name, classroom, teacher } = course;
  const period = `第 ${start === end ? start : `${start}–${end}`} 节`;
  const time = validRange(course.start_time, course.end_time) ? `${course.start_time}–${course.end_time}` : course.start_time;
  const location = [classroom, teaching ? '' : teacher, campusPointsLabel(course.points)].filter(Boolean).join(' · ');
  card.title = [name, time, period, location].filter(Boolean).join(' · ');
  card.append(element('span', 'schedule-course-period', time ? `${time} · ${period}` : period),
    element('strong', '', name), element('span', 'schedule-course-detail', location));
  return card;
}

function calendarCard(entry, teaching, openActivity, editEvent) {
  if (entry.kind === 'course') return courseCard(entry, teaching);
  if (entry.kind !== 'activity') {
    const card = entry.kind === 'custom' ? button('', 'schedule-course schedule-custom', () => editEvent(entry.id))
      : element('div', 'schedule-course schedule-classroom');
    if (entry.kind === 'custom') card.dataset.focusKey = `schedule-custom-${entry.id}`;
    const time = segmentTime(entry);
    card.title = [entry.title, time, entry.kind === 'custom' && entry.location].filter(Boolean).join(' · ');
    card.append(element('span', 'schedule-course-period', `${time} · ${eventLabels[entry.kind]}`), element('strong', '',
      entry.kind === 'classroom' ? entry.location : entry.title));
    if (entry.kind === 'custom' && entry.location) card.append(element('span', 'schedule-course-detail', entry.location));
    return card;
  }
  const card = button('', 'schedule-course schedule-activity', () => openActivity(entry.id));
  card.dataset.activityId = entry.id;
  card.dataset.focusKey = `schedule-activity-${entry.id}`;
  card.setAttribute('aria-controls', 'schedule-activity-detail');
  const time = segmentTime(entry);
  const status = campusActivityStatus(entry.registration);
  const detail = [entry.location, campusPointsLabel(entry.points)].filter(Boolean).join(' · ');
  card.title = [entry.name, time, detail, status].filter(Boolean).join(' · ');
  card.append(element('span', 'schedule-course-period', `${time} · 校园活动`),
    element('strong', '', entry.name));
  if (detail) card.append(element('span', 'schedule-course-detail', detail));
  if (status) card.append(element('span', 'schedule-activity-status', status));
  return card;
}

function segmentTime(entry) {
  if (entry.continues) return entry.segment_end === '24:00' ? '持续全天' : `持续至 ${entry.segment_end}`;
  return campusActivityRange(entry);
}

function sortedEntries(courses, dates, events) {
  const columns = courses.map(column => column.map(course => ({ ...course, kind: 'course' })));
  for (const event of events) {
    dates.forEach((date, day) => {
      if (date < event.date || date > event.end_date || date === event.end_date && event.end_time === '00:00') return;
      const continues = date > event.date;
      columns[day].push({ ...event, start_time: continues ? '00:00' : event.start_time, continues,
        segment_end: date < event.end_date ? '24:00' : event.end_time });
    });
  }
  return columns.map(column => column.sort(compareStart));
}

function markConflicts(entries) {
  let latestEnd = null;
  let highWater = '';
  // Entries are sorted by start. If there is an overlap, the earlier entry with
  // the latest end is enough to mark both, even with many coincident events.
  for (const entry of entries) {
    const start = entry.start_time;
    const end = entry.segment_end || entry.end_time;
    if (!start || !end || start >= end) continue;
    if (start < highWater) entry.conflict = latestEnd.conflict = true;
    if (end > highWater) {
      latestEnd = entry;
      highWater = end;
    }
  }
}

function createEventEditor(ui, state, add) {
  const form = element('form', 'campus-operation-form schedule-event-form');
  const title = element('h4');
  const fields = element('div', 'schedule-event-fields');
  const inputs = {};
  for (const [name, text, type, limit] of [
    ['title', '事件名称', 'text', 120], ['location', '地点（选填）', 'text', 180],
    ['starts_at', '开始时间（北京时间）', 'datetime-local'], ['ends_at', '结束时间（北京时间）', 'datetime-local'],
  ]) {
    const input = element('input');
    input.type = type;
    input.name = name;
    input.dataset.focusKey = `schedule-event-${name}`;
    input.required = name !== 'location';
    if (limit) input.maxLength = limit;
    const label = element('label', '', text);
    label.append(input);
    fields.append(label);
    inputs[name] = input;
  }
  const status = element('p', 'form-feedback');
  status.setAttribute('role', 'status');
  status.hidden = true;
  const controls = element('div', 'schedule-summary-actions');
  const submit = element('button', 'primary-button', '保存事件');
  submit.dataset.focusKey = 'schedule-event-save';
  submit.type = 'submit';
  const dismiss = button('取消', 'outline-button', close);
  dismiss.dataset.focusKey = 'schedule-event-dismiss';
  const remove = button('删除事件', 'outline-button schedule-event-delete', async () => {
    const { id, revision } = state.eventDraft;
    if (!window.confirm('删除这项自定义事件？')) return;
    const result = await ui.write(`${eventsPath}/${id}?${new URLSearchParams({ revision })}`, { method: 'DELETE' }, remove, '事件已删除。');
    if (result && state.eventDraft?.id === id) state.closeEventEditor?.();
  });
  remove.dataset.focusKey = 'schedule-event-delete';
  controls.append(submit, dismiss, remove);
  form.append(title, fields, status, controls);

  function refresh() {
    const draft = state.eventDraft;
    form.hidden = !draft;
    add.setAttribute('aria-expanded', String(Boolean(draft)));
    if (!draft) return;
    title.textContent = draft.revision ? '编辑自定义事件' : '新增自定义事件';
    remove.hidden = !draft.revision;
    status.hidden = true;
    for (const [name, input] of Object.entries(inputs)) input.value = draft[name] ?? '';
  }

  function close() {
    state.eventDraft = null;
    refresh();
    if (form.isConnected) add.focus({ preventScroll: true });
  }

  form.addEventListener('input', event => {
    state.eventDraft[event.target.name] = event.target.value;
    status.hidden = true;
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const { id, revision, title, location, starts_at, ends_at } = state.eventDraft;
    if (!title.trim() || ends_at <= starts_at) {
      status.dataset.tone = 'error';
      status.textContent = !title.trim() ? '请输入事件名称。' : '结束时间须晚于开始时间。';
      status.hidden = false;
      return;
    }
    const data = { title, location, starts_at, ends_at };
    const path = revision ? `${eventsPath}/${id}` : eventsPath;
    const result = await ui.write(path, revision ? { method: 'PUT', data: { ...data, revision } }
      : { method: 'POST', data: { ...data, id } }, submit, '事件已保存。');
    if (result && state.eventDraft?.id === id) state.closeEventEditor?.();
  });

  state.closeEventEditor = close;
  refresh();
  return { form, open(draft) {
    state.eventDraft = draft;
    refresh();
    inputs.title.focus();
  } };
}

function weekView(columns, dates, title, selectedDay, openDay, renderCard) {
  const table = element('table', 'schedule-table');
  const caption = table.createCaption();
  caption.className = 'sr-only';
  caption.textContent = `${title}日程`;
  const widths = element('colgroup');
  widths.append(element('col', 'schedule-axis'));
  const head = table.createTHead().insertRow();
  const axis = element('th', '', '时间');
  axis.scope = 'col';
  head.append(axis);
  days.forEach((day, index) => {
    const cell = element('th');
    cell.scope = 'col';
    const pick = button(dayLabel(index, dates[index]), 'schedule-day-link', () => openDay(index), `查看${day}日程`);
    if (index === selectedDay) pick.setAttribute('aria-current', 'true');
    cell.append(pick);
    head.append(cell);
    widths.append(element('col', index >= 5 ? 'schedule-weekend' : ''));
  });
  caption.after(widths);
  const body = table.createTBody();
  const slots = new Map();
  columns.forEach((entries, day) => {
    for (const entry of entries) {
      const hour = entry.start_time ? Number(entry.start_time.slice(0, 2)) : 24;
      if (!slots.has(hour)) slots.set(hour, days.map(() => []));
      slots.get(hour)[day].push(entry);
    }
  });
  for (const [hour, entriesByDay] of [...slots].sort(([a], [b]) => a - b)) {
    const row = body.insertRow();
    const label = element('th', '', hour === 24 ? '待定' : clock(hour * 60));
    label.scope = 'row';
    row.append(label);
    for (const entries of entriesByDay) {
      row.insertCell().append(...entries.map(renderCard));
    }
  }
  return table;
}

function compareStart(a, b) {
  return (a.start_time || '24:00').localeCompare(b.start_time || '24:00');
}

function dayView(entries, renderCard) {
  const agenda = element('ul', 'schedule-agenda');
  for (const entry of entries) {
    const row = element('li');
    row.append(renderCard(entry));
    agenda.append(row);
  }
  return agenda;
}

function minutes(time) {
  const [hour, minute] = time.split(':').map(Number);
  return hour * 60 + minute;
}

function clock(minutes) {
  return [Math.floor(minutes / 60), minutes % 60].map(value => String(value).padStart(2, '0')).join(':');
}

function hourView(entries, renderCard) {
  const view = element('div', 'schedule-hour-view');
  const timed = entries.filter(entry => entry.start_time);
  const unscheduled = entries.filter(entry => !entry.start_time);
  const pixelsPerMinute = 1.65;
  const lanes = [];
  const blocks = timed.map(entry => {
    const start = minutes(entry.start_time);
    const endTime = entry.segment_end || entry.end_time;
    const end = endTime && endTime > entry.start_time ? minutes(endTime) : start;
    // Give each card room to be read even if the activity only has a start time.
    const visibleUntil = Math.max(end, start + (entry.conflict ? 78 : 66));
    const free = lanes.findIndex(until => until <= start);
    const lane = free === -1 ? lanes.length : free;
    lanes[lane] = visibleUntil;
    return { start, visibleUntil, lane, entry };
  });
  if (blocks.length) {
    const start = Math.floor(blocks[0].start / 60) * 60;
    const end = Math.max(...blocks.map(block => block.visibleUntil));
    const hours = [];
    for (let hour = start; hour <= end && hour < 1440; hour += 60) hours.push(hour);
    const points = [...new Set([...hours, ...blocks.flatMap(block => [block.start, block.visibleUntil])])]
      .sort((a, b) => a - b);
    const rows = new Map(points.map((minute, index) => [minute, index + 1]));
    const tracks = points.slice(1).map((minute, index) => `${(minute - points[index]) * pixelsPerMinute}px`);
    tracks.push('auto');
    const grid = element('div', 'schedule-timeline');
    grid.style.gridTemplateColumns = `48px repeat(${lanes.length}, minmax(152px, 1fr))`;
    for (const hour of hours) {
      const text = clock(hour);
      const tick = element('time', 'schedule-hour-tick', text);
      tick.dateTime = text;
      tick.style.gridRow = String(rows.get(hour));
      grid.append(tick);
    }
    for (const block of blocks) {
      const slot = element('div', 'schedule-hour-slot');
      const row = rows.get(block.start);
      slot.style.gridRow = `${row} / ${rows.get(block.visibleUntil)}`;
      slot.style.gridColumn = String(block.lane + 2);
      if (block.entry.kind === 'activity') slot.classList.add('schedule-activity-start');
      slot.append(renderCard(block.entry));
      grid.append(slot);
    }
    grid.style.gridTemplateRows = tracks.join(' ');
    view.append(grid);
  }
  if (unscheduled.length) {
    view.append(element('h4', 'schedule-no-time', '时间待定'), dayView(unscheduled, renderCard));
  }
  return view;
}

function renderSchedule({ schedule: payload, activities, personalEvents, scheduleError, activityError, registrationError,
  restricted, eventsError, classroomUnavailable }, ui, state) {
  const courses = scheduleData(payload);
  const dates = weekDates(payload.week);
  const byId = new Map(activities.map(activity => [activity.id, { ...activity, kind: 'activity' }]));
  const customById = new Map(personalEvents.filter(event => event.kind === 'custom').map(event => [event.id, event]));
  const entriesByDay = sortedEntries(courses, dates, [...byId.values(), ...personalEvents]);
  entriesByDay.forEach(markConflicts);
  const teaching = payload.audience === 'teacher';
  state.setTeachingLabel(teaching);
  const courseLabel = teaching ? '授课安排' : '课程';
  const calendar = element('div', 'schedule-calendar');
  const summary = element('div', 'schedule-summary');
  const heading = element('div', 'schedule-heading');
  const title = element('h3');
  const count = element('span', 'schedule-count');
  count.setAttribute('role', 'status');
  heading.append(title, count);
  const picker = element('label', 'schedule-day-picker', '星期');
  const daySelect = element('select');
  daySelect.dataset.focusKey = 'schedule-day';
  days.forEach((_, index) => daySelect.add(new Option(dayLabel(index, dates[index]), String(index))));
  picker.append(daySelect);
  const sync = button('同步到日历', 'outline-button schedule-sync-button', ui.openCalendarSync);
  sync.dataset.focusKey = 'schedule-calendar-sync';
  const add = button('新增事件', 'outline-button schedule-sync-button', () => {
    if (state.eventDraft) {
      editor.form.elements.title.focus();
      return;
    }
    editor.open({ id: eventId(), starts_at: `${dates[state.day]}T09:00`, ends_at: `${dates[state.day]}T10:00` });
  });
  add.dataset.focusKey = 'schedule-add-event';
  add.setAttribute('aria-controls', 'schedule-event-form');
  const editor = createEventEditor(ui, state, add);
  editor.form.id = 'schedule-event-form';
  const actions = element('div', 'schedule-summary-actions');
  actions.append(picker, add, sync);
  summary.append(heading, actions);
  const swipe = element('p', 'schedule-swipe-hint', '左右滑动查看整周');
  const stage = element('div', 'schedule-stage');
  const detail = element('section', 'schedule-activity-detail');
  detail.id = 'schedule-activity-detail';
  detail.dataset.preserveScroll = 'activity-details';
  detail.setAttribute('aria-label', '活动详情');
  calendar.append(summary);
  const warnings = [
    scheduleError && '课表暂不可用。',
    restricted ? '当前校园身份无法查看校园活动。' : activityError && '校园活动暂不可用。',
    registrationError && '暂时无法同步报名状态。',
    eventsError ? '暂时无法读取自定义事件和教室预约。' : classroomUnavailable && '教室服务暂不可用，显示上次确认的预约。',
  ].filter(Boolean);
  if (warnings.length) calendar.append(element('p', 'dialog-intro', warnings.join('')));
  const conflicts = element('p', 'schedule-conflicts');
  conflicts.setAttribute('role', 'status');
  calendar.append(editor.form, conflicts, detail, swipe, stage);

  const week = weekLabel(payload.week);
  const renderCard = entry => {
    const card = calendarCard(entry, teaching, openActivity, editEvent);
    if (entry.conflict) {
      card.classList.add('schedule-course-conflict');
      card.append(element('span', 'schedule-conflict-badge', '时间冲突'));
    }
    return card;
  };
  const closeDetail = button('×', 'schedule-detail-close', () => {
    const previous = state.activityId;
    state.activityId = null;
    renderDetail();
    const trigger = [...stage.querySelectorAll('[data-activity-id]')].find(card => card.dataset.activityId === previous);
    (trigger || stage.querySelector('.schedule-scroll') || daySelect).focus({ preventScroll: true });
  }, '收起活动详情');
  closeDetail.dataset.focusKey = 'schedule-activity-close';
  function renderDetail() {
    const activity = byId.get(state.activityId);
    detail.hidden = !activity;
    for (const card of stage.querySelectorAll('[data-activity-id]')) {
      card.setAttribute('aria-expanded', String(card.dataset.activityId === state.activityId));
    }
    detail.replaceChildren();
    if (!activity) return;
    const list = element('ul', 'panel-list');
    list.append(renderCampusActivity(activity, ui, { registrationError }));
    const catalog = button('在校园活动中查看', 'link-button schedule-activity-catalog', () => ui.openActivity(activity.id));
    catalog.dataset.focusKey = 'schedule-activity-catalog';
    detail.append(closeDetail, list, catalog);
  }
  function openActivity(id) {
    state.activityId = id;
    renderDetail();
    closeDetail.focus({ preventScroll: true });
  }
  function editEvent(id) {
    const event = customById.get(id);
    if (state.eventDraft?.id === id) {
      editor.form.elements.title.focus();
      return;
    }
    editor.open({ id, revision: event.revision, title: event.title, location: event.location,
      starts_at: `${event.date}T${event.start_time}`, ends_at: `${event.end_date}T${event.end_time}` });
  }
  const openDay = day => {
    state.day = day;
    state.activityId = null;
    if (state.scale === 1) state.renderView();
    else state.setScale(1);
    daySelect.focus({ preventScroll: true });
  };
  state.renderView = direction => {
    const wholeWeek = state.scale === 0;
    const visible = state.showCourses ? entriesByDay : entriesByDay.map(column => column.filter(entry => entry.kind !== 'course'));
    const shown = wholeWeek ? visible.flat() : visible[state.day];
    const includesActivity = shown.some(entry => entry.kind === 'activity' && entry.id === state.activityId);
    if (state.activityId && !includesActivity) state.activityId = null;
    const uniqueCount = (rows, kind) => new Set(rows.filter(entry => entry.kind === kind).map(entry => entry.id)).size;
    const courseCount = !state.showCourses ? `${courseLabel}已隐藏`
      : payload.staff_verified === false ? '核验工号后可查看授课安排'
      : payload.student_verified === false ? '核验学号后可查看课表'
      : `${shown.filter(entry => entry.kind === 'course').length} 项${courseLabel}`;
    title.textContent = wholeWeek ? week : `${week} · ${dayLabel(state.day, dates[state.day])}`;
    count.textContent = `${courseCount} · ${uniqueCount(shown, 'activity')} 项已报名活动 · ` +
      `${uniqueCount(shown, 'classroom')} 项教室预约 · ${uniqueCount(shown, 'custom')} 项自定义事件`;
    const overlapping = (wholeWeek ? entriesByDay.flat() : entriesByDay[state.day]).filter(entry => entry.conflict);
    conflicts.hidden = overlapping.length === 0;
    const hiddenCourses = !state.showCourses && overlapping.some(entry => entry.kind === 'course');
    if (!overlapping.length) conflicts.textContent = '';
    else if (hiddenCourses && overlapping.every(entry => entry.kind === 'course')) {
      conflicts.textContent = '已隐藏的课表中有时间冲突。打开课表可查看。';
    } else {
      conflicts.textContent = `标记「时间冲突」的日程存在时段重叠。${hiddenCourses ? '冲突检查包含已隐藏的课表。' : ''}`;
    }
    picker.hidden = wholeWeek;
    swipe.hidden = !wholeWeek || shown.length === 0;
    daySelect.value = String(state.day);
    const scroll = element('div', 'schedule-scroll');
    scroll.tabIndex = 0;
    scroll.dataset.preserveScroll = 'schedule-grid';
    scroll.dataset.focusKey = 'schedule-grid';
    scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', `${title.textContent}日程`);
    if (direction) scroll.dataset.zoom = direction;
    if (wholeWeek) {
      if (shown.length) {
        scroll.append(weekView(visible, dates, week, state.day, openDay, renderCard));
      }
    } else if (shown.length) {
      scroll.append(state.scale === 1 ? dayView(shown, renderCard) : hourView(shown, renderCard));
    }
    stage.replaceChildren(scroll.childElementCount ? scroll : element('p', 'dialog-intro', '这段时间没有日程。'));
    renderDetail();
  };
  daySelect.addEventListener('change', () => {
    state.day = Number(daySelect.value);
    state.activityId = null;
    state.renderView();
  });
  state.renderView();
  return calendar;
}

export const schedulePanel = {
  title: '个人日程',
  intro: '课程、已报名活动、教室预约和自定义事件按北京时间排列。',
  controls: scheduleControls,
  poll: 15_000,
  campusActivities: true,
  async load(request, { week, role }, signal) {
    const emptySchedule = { source: 'minta_simulation', week, audience: role, items: [] };
    const [start] = weekDates(week);
    const [end] = weekDates(week + 1);
    const [schedule, activities, events] = await Promise.allSettled([
      (isTeachingWeek(week)
        ? request(`/v1/minta/schedule?${new URLSearchParams({ week })}`, { signal })
        : Promise.resolve(emptySchedule)).then(data => {
        if (data?.week !== week) throw new Error('日程周次与所选周次不一致，请刷新。');
        scheduleData(data);
        return data;
      }),
      loadCampusActivities(request, signal),
      request(`${eventsPath}?${new URLSearchParams({ start, end })}`, { signal }).then(data => {
        if (!Array.isArray(data?.items)) throw new Error('个人日程返回了无法读取的结果。');
        return data;
      }),
    ]);
    const signInError = [schedule, activities, events].find(result => result.reason?.status === 401);
    if (signInError) throw signInError.reason;
    if ([schedule, activities, events].every(result => result.status === 'rejected')) throw events.reason;
    return {
      schedule: schedule.value || emptySchedule,
      activityError: activities.status === 'rejected' || activities.value.activityError,
      registrationError: activities.value?.registrationError,
      restricted: activities.value?.restricted,
      activities: (activities.value?.activities || []).filter(activity => activity.registration?.state === 'active'),
      scheduleError: schedule.status === 'rejected',
      personalEvents: (events.value?.items || []).filter(item => Object.hasOwn(eventLabels, item.kind))
        .map(scheduledActivity).filter(event => event.end_time),
      eventsError: events.status === 'rejected',
      classroomUnavailable: events.value?.classroom_unavailable,
    };
  },
  render: renderSchedule,
};
