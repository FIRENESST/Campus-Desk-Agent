const state = { activities: [], registrations: [] };

const activitiesElement = document.querySelector('#activities');
const registrationsElement = document.querySelector('#registrations');
const registrationCount = document.querySelector('#registration-count');
const form = document.querySelector('#registration-form');
const activitySelect = document.querySelector('#activity-select');
const participantInput = document.querySelector('#participant-name');
const registerButton = document.querySelector('#register-button');
const refreshButton = document.querySelector('#refresh-button');
const notice = document.querySelector('#notice');
const activityDateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
});

function activityTimestamp(value) {
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value || '')) return NaN;
  return Date.parse(value.replace(' ', 'T') + (/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? '' : '+08:00'));
}

function activityRange(activity) {
  const start = activityTimestamp(activity.startsAt);
  const end = activityTimestamp(activity.endsAt);
  return activityDateTime.formatRange(start, end);
}

async function graphql(query, variables = {}) {
  const response = await fetch('/graphql', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  if (!response.ok) {
    throw new Error(`服务请求失败（HTTP ${response.status}）`);
  }
  const body = await response.json();
  if (body.errors?.length) {
    const error = body.errors[0];
    const code = error.extensions?.code ? ` [${error.extensions.code}]` : '';
    throw new Error(`${error.message}${code}`);
  }
  return body.data;
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle('error', isError);
  notice.hidden = false;
}

function renderActivities() {
  activitiesElement.replaceChildren();
  if (!state.activities.length) {
    activitiesElement.append(makeElement('p', 'empty-state', '本周暂无活动，稍后再来看看。'));
    return;
  }

  state.activities.forEach((activity, index) => {
    const card = makeElement('article', `activity-card${activity.remaining === 0 ? ' is-full' : ''}`);
    card.append(makeElement('span', 'card-number', String(index + 1).padStart(2, '0')));
    card.append(makeElement('h3', '', activity.name));
    card.append(makeElement('p', 'activity-description', activity.description));

    const meta = makeElement('div', 'activity-meta');
    meta.append(makeElement('span', '', `时间 / ${activityRange(activity)}`));
    meta.append(makeElement('span', '', `地点 / ${activity.location}`));
    card.append(meta);

    const capacity = makeElement('div', 'capacity-row');
    capacity.append(makeElement('span', '', `容量 ${activity.capacity} 人`));
    capacity.append(makeElement('span', '', activity.remaining ? `仅剩 ${activity.remaining} 席` : '本场已满'));
    card.append(capacity);

    const progress = makeElement('progress');
    progress.max = activity.capacity;
    progress.value = activity.capacity - activity.remaining;
    progress.setAttribute('aria-label', `已报名 ${activity.capacity - activity.remaining} 人，共 ${activity.capacity} 人`);
    card.append(progress);

    const button = makeElement('button', 'button', activity.remaining ? '报名这场' : '名额已满');
    button.type = 'button';
    button.disabled = activity.remaining === 0;
    button.addEventListener('click', () => {
      activitySelect.value = activity.id;
      participantInput.focus();
      form.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    card.append(button);
    activitiesElement.append(card);
  });
}

function renderActivityOptions() {
  const previous = activitySelect.value;
  activitySelect.replaceChildren();
  const placeholder = makeElement('option', '', '请选择活动');
  placeholder.value = '';
  activitySelect.append(placeholder);

  for (const activity of state.activities) {
    const option = makeElement('option', '', `${activity.name}（余 ${activity.remaining} 席）`);
    option.value = activity.id;
    option.disabled = activity.remaining === 0;
    activitySelect.append(option);
  }

  const previousActivity = state.activities.find((activity) => activity.id === previous && activity.remaining > 0);
  if (previousActivity) activitySelect.value = previous;
  registerButton.disabled = !state.activities.some((activity) => activity.remaining > 0);
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function renderRegistrations() {
  registrationsElement.replaceChildren();
  registrationCount.textContent = `${state.registrations.length} 条记录`;
  if (!state.registrations.length) {
    registrationsElement.append(makeElement('p', 'empty-state', '名册还是空白的，第一位报名的人会是你吗？'));
    return;
  }

  for (const registration of [...state.registrations].reverse()) {
    const item = makeElement('article', 'registration-item');
    item.append(makeElement('span', 'registration-avatar', registration.participantName.slice(0, 1)));

    const details = makeElement('div');
    details.append(makeElement('h3', '', registration.participantName));
    details.append(makeElement('p', '', `${registration.activityName} · ${formatTime(registration.createdAt)} 报名`));
    details.append(makeElement('small', '', registration.id));
    item.append(details);

    const cancelButton = makeElement('button', 'cancel-button', '取消报名');
    cancelButton.type = 'button';
    cancelButton.setAttribute('aria-label', `取消 ${registration.participantName} 的 ${registration.activityName} 报名`);
    cancelButton.addEventListener('click', async () => {
      cancelButton.disabled = true;
      try {
        await graphql(`mutation Cancel($registrationId: ID!) {
          cancelRegistration(registrationId: $registrationId) { id }
        }`, { registrationId: registration.id });
        await loadData();
        showNotice(`已取消 ${registration.participantName} 的报名，名额已释放。`);
      } catch (error) {
        cancelButton.disabled = false;
        showNotice(error.message, true);
      }
    });
    item.append(cancelButton);
    registrationsElement.append(item);
  }
}

function render() {
  renderActivities();
  renderActivityOptions();
  renderRegistrations();
}

async function loadData() {
  activitiesElement.setAttribute('aria-busy', 'true');
  try {
    const data = await graphql(`query ClubBoard {
      activities { id name description location startsAt endsAt capacity remaining }
      registrations { id activityId activityName participantName createdAt }
    }`);
    state.activities = data.activities;
    state.registrations = data.registrations;
    render();
  } finally {
    activitiesElement.setAttribute('aria-busy', 'false');
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  registerButton.disabled = true;
  try {
    const data = await graphql(`mutation Register($activityId: ID!, $participantName: String!) {
      registerActivity(activityId: $activityId, participantName: $participantName) { id }
    }`, {
      activityId: activitySelect.value,
      participantName: participantInput.value,
    });
    participantInput.value = '';
    await loadData();
    showNotice(`报名成功，记录编号是 ${data.registerActivity.id}。`);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    registerButton.disabled = !state.activities.some((activity) => activity.remaining > 0);
  }
});

refreshButton.addEventListener('click', async () => {
  refreshButton.disabled = true;
  try {
    await loadData();
    showNotice('活动与名册已更新。');
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    refreshButton.disabled = false;
  }
});

loadData().catch((error) => {
  activitiesElement.replaceChildren(makeElement('p', 'empty-state', '活动读取失败，请稍后刷新。'));
  registrationsElement.replaceChildren(makeElement('p', 'empty-state', '名册读取失败，请稍后刷新。'));
  showNotice(error.message, true);
});
