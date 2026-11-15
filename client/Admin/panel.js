const byId = id => document.getElementById(id);
const editor = byId('editor');
const panel = byId('panel');
const sectionPicker = byId('section-picker');
const adminContent = byId('admin-content');
const wereadDialog = byId('weread-login');
const weread = { timer: null, sequence: 0, starting: false, notified: false, session: 'checking', encoded: '', image: '' };
const rumorCollection = { timer: null, request: null, statuses: null };
const rumorSources = [
  ['campus_wall', '校园墙', '请在微信打开「云上校友圈」，在原采集器重新授权，再更新服务端的采集配置。'],
  ['xiaohongshu', '小红书', '请使用授权账号扫码登录。登录成功后会自动开始采集。'],
];
const rumorStatusLabels = {
  checking: '检测中', needs_login: '需要登录', awaiting_scan: '等待扫码', collecting: '正在采集',
  ready: '已授权 · 采集正常', check_login: '请核对采集账号授权',
  unavailable: '暂时无法核对授权或采集状态', disabled: '采集已停用',
};
const wereadSessionLabels = {
  online: '已登录', needs_login: '待扫码', checking: '检测中', unknown: '待核验', unavailable: '暂不可用',
};
const wereadActivePhases = new Set(['waiting_scan', 'scanned']);
const wereadLabels = {
  checking: '正在检查微信读书登录状态…', online: '微信读书已登录，校园日报可以读取已关注的公众号。',
  needs_login: '微信读书尚未登录或登录已过期。请使用微信扫码。', unknown: '暂时无法确认微信读书的登录状态。',
};
const routingPageSize = 25;
const photoReviewPageSize = 20;
const routingDirectLabel = '不路由直接回答';
const routingNumber = new Intl.NumberFormat('zh-CN');
const routingDate = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
});
const newRoutingFilters = () => ({ days: 14, status: 'all', agent: '', origin: '', day: '', q: '', offset: 0 });
const state = {
  session: null, view: 'processes', version: 0, idle: null, scope: 'campus', campus: 'classroom', week: 1,
  dataset: 'users', parent: '', search: '', offset: 0,
  campusProfileSearch: '', campusProfileOffset: 0, clubReviewOffset: 0, clubArchiveOffset: 0,
  photoStatus: 'pending', photoPlace: '', photoPlaceOffset: 0, photoOffset: 0,
  routingFilters: newRoutingFilters(),
};

const views = {
  processes: ['运行总览', '查看主管理器登记的进程和后台依赖。', processes],
  data: ['用户与业务数据', '按用户 ID、邮箱或关联 ID 定位 PostgreSQL 记录。', database],
  campus: ['校园服务后端', '管理服务开放范围、教室状态和社团审核。', campus],
  wechat: ['公众号采集', '校园日报通过微信读书采集已关注的公众号。', wechatCollection],
  rumor: ['校园传闻管理', '处理待核线索、核对官方来源和发布复核结论。', rumorManagement],
  photos: ['地点照片审核', '按地点查看和审核校园地图的照片投稿。', campusPhotos],
  rag: ['RAG 资料库', '按资料范围查看、导入、删除和检索文档。', rag],
  capabilities: ['SubSkill 与 MCP', '查看运行中的注册关系与校园办理动作。', capabilities],
  routing: ['自适应路由反馈', '查看实际派发、标注样本和试算路由建议。', routing],
  cas: ['校园资料核验', '核验校园资料并管理邮箱与校园身份的绑定。', campusProfileReview],
  credentials: ['教师凭据', '按教师邮箱查询、签发、修改或撤销电子凭据。', credentials],
  audit: ['管理审计', '查看最近的在线操作和本地管理员变更。', audit],
};

function element(tag, text = '', className = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function button(label, callback, className = '') {
  const item = element('button', label, className);
  item.type = 'button';
  item.addEventListener('click', callback);
  return item;
}

function card(title, ...children) {
  const section = element('section', '', 'card');
  if (title) section.append(element('h2', title));
  section.append(...children);
  return section;
}

function rumorManagement(target) {
  const link = element('a', '打开谣言防控管理台');
  link.href = '/admin/rumor';
  const sources = element('div', '', 'split');
  sources.id = 'rumor-collection-status';
  target.append(card('采集授权', sources), card('监测与复核', link));
  renderRumorStatus();
  void refreshRumorStatus();
}

function renderRumorStatus() {
  const statuses = rumorCollection.statuses;
  const signingIn = new Set(['needs_login', 'awaiting_scan']);
  const attention = rumorSources.filter(([key]) => signingIn.has(statuses?.[key]?.status)).map(([, label]) => label);
  const verifyWall = statuses?.campus_wall?.status === 'check_login';
  const alert = byId('rumor-login-notice');
  const message = byId('rumor-login-message');
  const reminder = [attention.length ? `请完成${attention.join('和')}的采集登录授权。` : '',
    verifyWall ? '请核对校园墙的登录状态，授权过期时请重新登录。' : ''].filter(Boolean).join(' ');
  if (message.textContent !== reminder) message.textContent = reminder;
  alert.hidden = !reminder || !state.session;

  const panel = byId('rumor-collection-status');
  if (!panel) return;
  const revision = JSON.stringify(statuses);
  if (panel.dataset.status === revision) return;
  panel.dataset.status = revision;
  panel.replaceChildren(...rumorSources.map(([key, name, instruction]) => {
    const source = statuses?.[key];
    const status = source?.status || (statuses ? 'unavailable' : 'checking');
    const section = element('section', '', 'card collector-summary');
    section.append(element('h3', name), element('p', rumorStatusLabels[status] || rumorStatusLabels.unavailable, 'muted'));
    if (signingIn.has(status) || status === 'check_login') section.append(element('p', instruction));
    if (source?.last_success_at) {
      const completed = new Date(source.last_success_at);
      if (Number.isFinite(completed.valueOf())) {
        section.append(element('p', `上次采集 ${routingDate.format(completed)} · ${source.last_count ?? 0} 条`, 'muted'));
      }
    }
    if (key === 'xiaohongshu') {
      const login = element('a', '打开小红书扫码入口');
      login.href = '/admin/rumor/radar#xhsLogin';
      section.append(login);
    }
    return section;
  }));
}

async function refreshRumorStatus() {
  const session = state.session;
  if (!session || rumorCollection.request) return;
  clearTimeout(rumorCollection.timer);
  const request = new AbortController();
  rumorCollection.request = request;
  const timeout = setTimeout(() => request.abort(), 8000);
  try {
    const response = await fetch('/admin/rumor/api/collection_status', {
      headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin', signal: request.signal,
    });
    const statuses = response.ok ? await response.json() : {};
    if (state.session === session) rumorCollection.statuses = statuses;
  } catch {
    if (state.session === session && !rumorCollection.statuses) rumorCollection.statuses = {};
  } finally {
    clearTimeout(timeout);
    if (rumorCollection.request === request) rumorCollection.request = null;
    if (state.session === session) {
      renderRumorStatus();
      rumorCollection.timer = setTimeout(refreshRumorStatus, 15000);
    }
  }
}

function group(...children) {
  const area = element('div', '', 'actions');
  area.append(...children);
  return area;
}

function label(name, input) {
  const wrapper = element('label', name);
  wrapper.append(input);
  return wrapper;
}

function input(type, value = '', maxLength) {
  const field = element('input');
  field.type = type;
  field.value = value ?? '';
  if (maxLength) field.maxLength = maxLength;
  return field;
}

function select(options, value) {
  const field = element('select');
  for (const [key, title] of options) {
    const option = element('option', title);
    option.value = key;
    field.append(option);
  }
  field.value = value;
  return field;
}

function notice(item, message = '', error = false) {
  item.textContent = message;
  item.classList.toggle('error', error);
}

function valueText(value) {
  if (value === true) return '是';
  if (value === false) return '否';
  if (Array.isArray(value)) return value.join('、') || '—';
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

function table(columns, rows, rowActions) {
  if (!rows?.length) return card('', element('p', '暂无记录', 'muted'));
  const wrap = element('div', '', 'table-wrap');
  wrap.tabIndex = 0;
  wrap.setAttribute('role', 'region');
  wrap.setAttribute('aria-label', `表格 · ${columns.map(([, name]) => name).join('、')}`);
  const list = element('table');
  const head = list.createTHead().insertRow();
  for (const [, name] of columns) {
    const cell = element('th', name);
    cell.scope = 'col';
    head.append(cell);
  }
  if (rowActions) head.append(element('th', '操作'));
  const body = list.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const [field] of columns) {
      const value = typeof field === 'function' ? field(row) : row[field];
      const cell = line.insertCell();
      if (value instanceof Node) cell.append(value);
      else cell.textContent = valueText(value);
    }
    if (rowActions) {
      const cell = line.insertCell();
      cell.className = 'row-actions';
      cell.append(group(...rowActions(row)));
    }
  }
  wrap.append(list);
  return wrap;
}

function output(data) {
  return element('pre', typeof data === 'string' ? data : JSON.stringify(data, null, 2));
}

function sessionExpired() {
  clearTimeout(state.idle);
  state.idle = setTimeout(() => setSession(null, '管理会话已闲置，请重新登录。'), state.session.idle_seconds * 1000);
}

function setSession(session, message = '') {
  state.session = session;
  clearTimeout(rumorCollection.timer);
  rumorCollection.request?.abort();
  rumorCollection.request = rumorCollection.statuses = null;
  renderRumorStatus();
  clearTimeout(weread.timer);
  ++weread.sequence;
  weread.starting = weread.notified = false;
  setWeReadSession('checking');
  if (wereadDialog.open) wereadDialog.close();
  setWeReadImage('');
  document.body.classList.toggle('workspace-open', Boolean(session));
  byId('sign-in').hidden = Boolean(session);
  byId('workspace').hidden = !session;
  byId('logout').hidden = !session;
  byId('weread-status').hidden = !session;
  const identity = byId('identity');
  identity.hidden = !session;
  identity.textContent = session?.name || '';
  notice(byId('login-feedback'), message);
  if (session) {
    sessionExpired();
    void render();
    if (state.view !== 'wechat') void refreshWeRead();
    void refreshRumorStatus();
  } else {
    ++state.version;
    state.campusProfileSearch = '';
    state.campusProfileOffset = state.clubReviewOffset = state.clubArchiveOffset = 0;
    state.photoStatus = 'pending';
    state.photoPlace = '';
    state.photoPlaceOffset = state.photoOffset = 0;
    state.routingFilters = newRoutingFilters();
    clearTimeout(state.idle);
    panel.replaceChildren();
    if (editor.open) editor.close();
  }
}

function uploadJson(url, method, headers, body, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const failed = () => reject(new Error('上传连接中断，请核对资料列表后重试。'));
    request.open(method, url);
    for (const [name, value] of Object.entries(headers)) request.setRequestHeader(name, value);
    request.upload.addEventListener('progress', event => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    });
    request.upload.addEventListener('load', () => onProgress(null));
    request.addEventListener('load', () => {
      if (!request.status) return failed();
      resolve(new Response(request.responseText || null, { status: request.status }));
    });
    request.addEventListener('error', failed);
    request.addEventListener('abort', failed);
    request.send(body);
  });
}

async function api(path, { method = 'GET', data, photo = false, onUpload, background = false } = {}) {
  const session = state.session;
  const headers = { Accept: photo ? 'image/jpeg' : 'application/json' };
  if (session?.csrf) headers['X-Campus-Admin-CSRF'] = session.csrf;
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const url = '/admin/api' + path;
  const payload = data === undefined ? undefined : JSON.stringify(data);
  const response = onUpload ? await uploadJson(url, method, headers, payload, onUpload)
    : await fetch(url, { method, headers, credentials: 'same-origin', cache: 'no-store', body: payload });
  const body = photo && response.ok ? await response.blob()
    : response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.error?.message || '服务暂不可用，请稍后重试。';
    if (session && state.session === session &&
        (response.status === 401 || ['admin_csrf_denied', 'admin_campus_required'].includes(body.error?.code))) setSession(null, message);
    throw new Error(message);
  }
  if (session && state.session === session && !background) sessionExpired();
  if (photo && body.type !== 'image/jpeg') throw new Error('照片预览不可用。');
  return body;
}

function setWeReadImage(encoded) {
  if (weread.encoded === encoded) return;
  const image = byId('weread-qr');
  if (weread.image) URL.revokeObjectURL(weread.image);
  weread.encoded = encoded;
  weread.image = '';
  image.hidden = true;
  image.removeAttribute('src');
  if (encoded) {
    const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
    weread.image = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
    image.src = weread.image;
    image.hidden = false;
  }
}

function setWeReadSession(session) {
  if (!Object.hasOwn(wereadSessionLabels, session)) session = 'unknown';
  weread.session = session;
  const label = wereadSessionLabels[session];
  byId('weread-status').textContent = `微信读书 · ${label}`;
  const badge = byId('weread-collection-status');
  if (badge && badge.dataset.session !== session) {
    badge.dataset.session = session;
    badge.textContent = label;
  }
}

function openWeReadDialog(start = false) {
  if (!state.session || wereadDialog.open) return;
  weread.notified = true;
  notice(byId('weread-feedback'), wereadLabels.checking);
  byId('weread-start').hidden = true;
  wereadDialog.showModal();
  void refreshWeRead(start);
}

function wechatCollection(target) {
  const badge = element('span', '', 'badge weread-session-badge');
  badge.id = 'weread-collection-status';
  badge.setAttribute('role', 'status');
  const info = element('div');
  info.append(element('h2', '微信读书登录状态'), badge);
  const section = card('', info, button('打开登录二维码', () => openWeReadDialog(true), 'primary'));
  section.classList.add('weread-collection');
  target.append(section);
  setWeReadSession(weread.session);
  return refreshWeRead();
}

function showWeReadStatus({ session, phase, qr_png_base64: qr = '' }) {
  setWeReadSession(session);
  if (session === 'online') weread.notified = false;
  if (session === 'needs_login' && !weread.notified && !document.querySelector('dialog[open]')) {
    wereadDialog.showModal();
  }
  if (!wereadDialog.open) return;
  if (session === 'needs_login') weread.notified = true;
  const text = {
    waiting_scan: qr ? '用微信扫描二维码，并在手机上确认登录。' : '正在准备二维码…',
    scanned: '已扫码，请在手机上确认登录。',
    success: session === 'checking' ? '扫码已完成，正在核验登录状态…' : wereadLabels[session],
    failed: '上次扫码已超时或失败，可以重新生成二维码。',
  }[phase] || wereadLabels[session];
  notice(byId('weread-feedback'), text, phase === 'failed');
  byId('weread-start').hidden = wereadActivePhases.has(phase);
  setWeReadImage(phase === 'waiting_scan' ? qr : '');
}

async function refreshWeRead(start = false) {
  if (!state.session || weread.starting || (document.hidden && !start) || (start && !wereadDialog.open)) return;
  clearTimeout(weread.timer);
  const sequence = ++weread.sequence;
  let delay = wereadDialog.open ? 2500 : 60_000;
  if (start) {
    weread.starting = true;
    byId('weread-start').disabled = true;
    notice(byId('weread-feedback'), '正在准备二维码…');
  }
  try {
    const path = '/digest/weread' + (wereadDialog.open ? '/login' : '');
    const status = await api(path, { method: start ? 'POST' : 'GET', background: !start });
    if (sequence !== weread.sequence) return;
    showWeReadStatus(status);
    if (status.session === 'checking' || (wereadDialog.open && wereadActivePhases.has(status.phase)) ||
        (status.session === 'needs_login' && !weread.notified)) delay = 2500;
    else delay = 60_000;
  } catch (error) {
    if (sequence !== weread.sequence) return;
    if (!start) setWeReadSession('unavailable');
    notice(byId('weread-feedback'), error.message, true);
    byId('weread-start').hidden = false;
    setWeReadImage('');
    delay = 60_000;
  } finally {
    if (sequence === weread.sequence) {
      weread.starting = false;
      byId('weread-start').disabled = false;
      weread.timer = setTimeout(refreshWeRead, delay);
    }
  }
}

async function render() {
  if (!state.session) return;
  const version = ++state.version;
  const previousScroll = adminContent.scrollTop;
  const refresh = byId('refresh');
  const [title, description, load] = views[state.view];
  byId('view-title').textContent = title;
  byId('view-summary').textContent = description;
  for (const control of byId('sections').querySelectorAll('button')) {
    if (control.dataset.view === state.view) control.setAttribute('aria-current', 'page');
    else control.removeAttribute('aria-current');
  }
  sectionPicker.value = state.view;
  refresh.disabled = true;
  panel.setAttribute('aria-busy', 'true');
  notice(byId('feedback'), '正在读取…');
  const target = element('div', '', 'view-stack');
  panel.replaceChildren(target);
  try {
    await load(target, version);
    if (version === state.version) notice(byId('feedback'));
  } catch (error) {
    if (version === state.version) notice(byId('feedback'), error.message, true);
  } finally {
    if (version === state.version) {
      refresh.disabled = false;
      panel.removeAttribute('aria-busy');
      adminContent.scrollTop = previousScroll;
    }
  }
}

async function action(callback, success = '操作完成。', refresh = true) {
  try {
    const result = await callback();
    if (refresh) await render();
    notice(byId('feedback'), typeof success === 'function' ? success(result) : success);
  } catch (error) {
    notice(byId('feedback'), error.message, true);
  }
}

let editHandler;
function edit(title, fields, save) {
  byId('editor-title').textContent = title;
  const content = byId('editor-fields');
  const controls = {};
  content.replaceChildren();
  for (const field of fields) {
    const [key, caption, value, config = {}] = field;
    if (config.checklist) {
      const choices = element('fieldset', '', 'routing-role-options');
      choices.append(element('legend', caption));
      const checkboxes = config.options.map(([id, name]) => {
        const checkbox = input('checkbox', id);
        checkbox.className = 'routing-check';
        checkbox.checked = value.includes(id);
        const item = element('label');
        item.append(checkbox, element('span', name));
        choices.append(item);
        return checkbox;
      });
      if (config.exclusive) {
        choices.addEventListener('change', ({ target }) => {
          if (!target.checked) return;
          for (const checkbox of checkboxes) {
            if (checkbox !== target && (target.value === config.exclusive || checkbox.value === config.exclusive)) {
              checkbox.checked = false;
            }
          }
        });
      }
      controls[key] = () => checkboxes.filter(checkbox => checkbox.checked).map(checkbox => checkbox.value);
      content.append(choices);
      continue;
    }
    let control;
    if (config.options) {
      control = select(config.options, value);
      if (config.multiple) {
        control.multiple = true;
        control.size = 5;
        for (const option of control.options) option.selected = value.includes(option.value);
      }
    }
    else if (config.multiline) {
      control = element('textarea');
      control.value = value ?? '';
      if (config.max) control.maxLength = config.max;
    } else control = input(config.type || 'text', value, config.max);
    if (config.min) control.minLength = config.min;
    if (config.autocomplete) control.autocomplete = config.autocomplete;
    control.required = config.required ?? true;
    controls[key] = () => control.multiple ? Array.from(control.selectedOptions, option => option.value) : control.value;
    content.append(label(caption, control));
  }
  editHandler = () => save(Object.fromEntries(Object.entries(controls).map(([key, read]) => [key, read()])));
  notice(byId('editor-feedback'));
  editor.showModal();
}

byId('editor-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter || event.currentTarget.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    await editHandler();
    if (editor.open) editor.close();
    await render();
    notice(byId('feedback'), '已保存。');
  } catch (error) {
    notice(byId('editor-feedback'), error.message, true);
  } finally {
    submit.disabled = false;
  }
});
byId('editor-cancel').addEventListener('click', () => editor.close());
editor.addEventListener('close', () => {
  if (!editor.open) {
    editHandler = null;
    byId('editor-fields').replaceChildren();
  }
});

const processStates = {
  ready: '就绪', running: '运行中', external: '外部运行', starting: '启动中', stopped: '已停止', exited: '已退出',
  queued: '等待执行', completed: '已完成', failed: '未完成',
};
const processActions = { start: '启动', stop: '停止', restart: '重启' };
const campusProcesses = ['mintagw', 'sim-classroom', 'sim-gymnasium', 'sim-club', 'sim-library', 'sim-schedule', 'teacher-credentials'];

function processTable(rows) {
  return table([['name', '进程'], [row => processStates[row.status] || row.status, '状态'], ['pid', 'PID']], rows, row =>
    (row.actions || []).filter(operation => row.status !== 'external' &&
      (operation === 'start' ? ['stopped', 'exited'].includes(row.status) : ['ready', 'starting', 'running'].includes(row.status)))
      .map(operation => button(processActions[operation], () => {
        const name = processActions[operation] + ' ' + row.name;
        if (!window.confirm(name + '？主管理器会检查进程依赖。')) return;
        void action(async () => {
          const accepted = await api('/runtime/processes/' + row.name, { method: 'POST', data: { action: operation } });
          const version = state.version;
          void waitForProcess(accepted.job.id, version);
          return accepted;
        }, '已提交给主管理器。', false);
      }, operation === 'stop' ? 'danger' : '')));
}

async function waitForProcess(id, version) {
  for (let attempt = 0; attempt < 24; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 2500));
    if (state.version !== version || !state.session) return;
    try {
      const { job } = await api('/runtime/processes');
      if (job?.id === id && ['completed', 'failed'].includes(job.state)) {
        await render();
        notice(byId('feedback'), job.message || '进程操作已完成。', job.state === 'failed');
        return;
      }
    } catch (error) {
      if (state.version === version) notice(byId('feedback'), error.message, true);
      return;
    }
  }
}

async function processes(target) {
  const { items, job } = await api('/runtime/processes');
  const ready = items.filter(item => ['ready', 'running', 'external'].includes(item.status)).length;
  target.append(card('登记进程', element('p', `${ready} / ${items.length} 个进程在线`, 'muted')));
  if (job) target.append(card('最近的操作', element('p', `${processActions[job.action]} ${job.service} · ${processStates[job.state]}`),
    ...(job.message ? [element('p', job.message, 'muted')] : [])));
  target.append(processTable(items));
}

const datasets = {
  users: ['用户', [['id', '用户 ID'], ['email', '邮箱'], ['display_name', '显示名称'], ['role', '角色'],
    ['academic_title', '职称'], ['verified_at', '认证时间'], [row => row.is_active ? '启用' : '停用', '状态']]],
  instances: ['实例', [['id', '实例 ID'], ['user_id', '用户 ID'], ['name', '名称'], ['status', '状态'], ['is_default', '默认']]],
  conversations: ['会话', [['id', '会话 ID'], ['user_id', '用户 ID'], ['instance_id', '实例 ID'], ['title', '标题'], ['updated_at', '更新']]],
  messages: ['消息', [['id', '消息 ID'], ['role', '发送方'], ['content', '内容'], ['created_at', '时间']]],
  runs: ['执行任务', [['id', '任务 ID'], ['conversation_id', '会话 ID'], ['status', '状态'], ['result_status', '结果'],
    ['attempt', '尝试次数'], ['created_at', '创建时间']]],
  sessions: ['登录会话', [['id', '会话 ID'], ['user_id', '用户 ID'], ['auth_method', '登录方式'], ['expires_at', '到期'],
    ['revoked_at', '撤销时间']]],
  identities: ['校园身份绑定', [['id', '绑定 ID'], ['user_id', '用户 ID'], ['protocol', '协议'], ['created_at', '绑定时间']]],
  minta: ['校务办理', [['id', '办理 ID'], ['user_id', '用户 ID'], ['service', '业务'], ['resource_name', '资源'],
    ['state', '状态'], ['updated_at', '更新时间']]],
  'user-audit': ['用户审计', [['user_id', '用户 ID'], ['action', '动作'], ['outcome', '结果'], ['created_at', '时间']]],
};

function openDataset(dataset, parent = '') {
  state.dataset = dataset;
  state.parent = parent;
  state.search = '';
  state.offset = 0;
  void render();
}

function confirmed(message, callback, success, refresh = true) {
  return () => { if (window.confirm(message)) void action(callback, success, refresh); };
}

function recordActions(row, options) {
  const kind = state.dataset;
  const path = '/data/' + kind + '/' + row.id;
  const patch = (data, extra = '') => api(path + extra, { method: 'PATCH', data });
  const deleteRecord = button('删除', confirmed('确认删除这条记录及其关联的会话数据？',
    () => api(path, { method: 'DELETE' })), 'danger');
  if (kind === 'users') {
    const controls = [
      button('改名', () => edit('修改显示名称', [['display_name', '显示名称', row.display_name, { required: false, max: 50 }]], patch)),
      button(row.is_active ? '停用' : '启用', confirmed(row.is_active ? '停用该用户并撤销其登录会话？' : '启用该用户？',
        () => patch({ is_active: !row.is_active })), row.is_active ? 'danger' : ''),
      button('查看实例', () => openDataset('instances', row.id)),
      button('查看会话', () => openDataset('conversations', row.id)),
      button('撤销登录', confirmed('撤销该用户的所有登录会话？', () => api(path + '/revoke', { method: 'POST' }))),
    ];
    if (options.role_editable) controls.push(button('角色与职称', () => edit('角色与职称', [
      ['role', '角色', row.role, { options: [['student', '学生'], ['teacher', '教师']] }],
      ['academic_title', '教师职称', row.academic_title || '', { options: [['', '无'], ...options.titles.map(title => [title, title])], required: false }],
    ], data => patch({ ...data, academic_title: data.role === 'teacher' ? data.academic_title || null : null }, '/role'))));
    return controls;
  }
  if (kind === 'instances') return [
    button('编辑', () => edit('编辑实例', [
      ['name', '名称', row.name, { max: 100 }],
      ['status', '状态', row.status, { options: [['active', '启用'], ['paused', '暂停']] }],
      ['make_default', '设为默认', 'false', { options: [['false', '保持原设置'], ['true', '设为默认']] }],
    ], data => patch({ ...data, make_default: data.make_default === 'true' }))),
    deleteRecord,
  ];
  if (kind === 'conversations') return [
    button('查看消息', () => openDataset('messages', row.id)),
    button('查看任务', () => openDataset('runs', row.id)),
    button('改名', () => edit('修改会话标题', [['title', '会话标题', row.title, { max: 200 }]], patch)),
    deleteRecord,
  ];
  if (kind === 'sessions' && !row.revoked_at) {
    return [button('撤销', confirmed('撤销该登录会话？', () => api(path + '/revoke', { method: 'POST' })), 'danger')];
  }
  if (kind === 'minta') {
    const status = result => '办理状态为 ' + result.state + '。';
    const controls = [button('核对状态', () => void action(() => api(path + '/reconcile', { method: 'POST' }), status))];
    if (['active', 'close_uncertain'].includes(row.state)) {
      controls.push(button('取消或归还', confirmed('确认向校园后端取消该预约、报名或办理归还？',
        () => api(path + '/close', { method: 'POST' }), status), 'danger'));
    }
    return controls;
  }
  return [];
}

async function database(target) {
  const kind = state.dataset;
  const selector = select(Object.entries(datasets).map(([key, [title]]) => [key, title]), kind);
  selector.addEventListener('change', () => openDataset(selector.value));
  const query = input('search', kind === 'users' ? state.search : state.parent, 160);
  query.placeholder = kind === 'users' ? '邮箱、名称或用户 ID' : kind === 'messages' || kind === 'runs' ? '会话 ID' : '用户 ID';
  const toolbar = element('form', '', 'toolbar');
  const filter = kind === 'users' ? '查找用户' : '关联 ID';
  toolbar.append(label('数据类型', selector), label(filter, query), element('button', '查询'));
  toolbar.addEventListener('submit', event => {
    event.preventDefault();
    state.offset = 0;
    if (kind === 'users') state.search = query.value.trim();
    else state.parent = query.value.trim();
    void render();
  });
  if (kind === 'instances') toolbar.append(button('新建实例', () => {
    const owner = query.value.trim();
    if (!owner) return notice(byId('feedback'), '请先填写所属用户 ID。', true);
    edit('新建实例', [['name', '实例名称', '', { max: 100 }]],
      data => api('/data/instances?parent=' + encodeURIComponent(owner), { method: 'POST', data }));
  }, 'primary'));
  target.append(card('', toolbar));
  if (kind === 'messages' && !state.parent) {
    target.append(card('', element('p', '请填写会话 ID，或在会话列表点击「查看消息」。', 'muted')));
    return;
  }
  const params = new URLSearchParams({ offset: state.offset });
  if (state.search) params.set('q', state.search);
  if (state.parent) params.set('parent', state.parent);
  const [data, options] = await Promise.all([
    api('/data/' + kind + '?' + params),
    kind === 'users' ? api('/data/options').catch(() => ({})) : {},
  ]);
  const editable = ['users', 'instances', 'conversations', 'sessions', 'minta'].includes(kind);
  target.append(table(datasets[kind][1], data.items, editable ? row => recordActions(row, options) : null));
  const pages = group();
  if (state.offset) pages.append(button('上一页', () => { state.offset -= 40; void render(); }));
  if (data.next_offset !== null) pages.append(button('下一页', () => { state.offset = data.next_offset; void render(); }));
  if (pages.childElementCount) target.append(pages);
}

const reviewLabels = { pending: '待审核', approved: '已通过', rejected: '未通过' };

function pagination(offset, next, change, size = 40) {
  const previous = button('上一页', () => change(Math.max(0, offset - size)));
  const more = button('下一页', () => change(next));
  previous.disabled = !offset;
  more.disabled = next == null;
  return group(previous, more);
}

function editCampusRule(path, title, rule, grades, room) {
  const audience = rule?.audience || {};
  const fields = [];
  for (const [prefix, name] of [['allow', '允许'], ['deny', '禁止']]) {
    fields.push([`${prefix}_grades`, `${name}年级（可多选）`, audience[`${prefix}_grades`] || [],
      { options: Object.entries(grades), multiple: true, required: false }]);
    for (const [key, label] of [['colleges', '学院'], ['users', '用户 UUID']]) {
      const field = `${prefix}_${key}`;
      fields.push([field, `${name}${label}（逗号或换行分隔）`, (audience[field] || []).join('\n'),
        { multiline: true, max: 3000, required: false }]);
    }
  }
  if (room) fields.unshift(['blocked', '教室状态', String(Boolean(rule?.blocked)), {
    options: [['false', '开放，按实际预约显示'], ['true', '设为占用，暂停预约']],
  }]);
  edit(title, fields, data => {
    const blocked = data.blocked === 'true';
    delete data.blocked;
    const updated = Object.fromEntries(Object.entries(data).map(([key, value]) => [key,
      Array.isArray(value) ? value : value.split(/[,，\n]/).map(item => item.trim()).filter(Boolean)]));
    return api(path, { method: 'PUT', data: { blocked, audience: updated } });
  });
}

async function clubReviewTable(target) {
  const { items, next_offset: next } = await api('/campus/clubs/review?offset=' + state.clubReviewOffset);
  target.append(table([['name', '社团'], ['category', '类别'], ['description', '介绍'], ['registrant', '登记人'],
    [row => row.archived ? '已归档' : reviewLabels[row.review_status], '状态']], items, row => {
    if (row.archived) return [];
    const path = '/campus/clubs/review/' + row.id;
    const review = status => api(path, { method: 'PATCH', data: { version: row.version, status } });
    return [
      row.review_status !== 'approved' && button('通过审核', confirmed('通过后按社团设置的开放范围公开。确认通过？', () => review('approved'))),
      row.review_status !== 'rejected' && button(row.review_status === 'approved' ? '撤下' : '驳回',
        confirmed('确认从公开列表隐藏该社团及其活动？', () => review('rejected')), 'danger'),
    ].filter(Boolean);
  }), pagination(state.clubReviewOffset, next, offset => { state.clubReviewOffset = offset; void render(); }));
}

async function clubArchiveTable(target) {
  const path = '/campus/clubs/archives';
  const { items, next_offset: next } = await api(path + '?offset=' + state.clubArchiveOffset);
  target.append(table([['club_name', '社团'], [row => row.activity_name || '整个社团', '归档对象'],
    ['registrant', '登记人']], items, row => {
    const submit = action => api(path + '/' + row.club_id, { method: 'PATCH', data: {
      action, version: row.version, ...(row.activity_id ? { activity_id: row.activity_id } : {}),
    } });
    const name = row.activity_name || row.club_name;
    const related = row.activity_id ? '相关报名会结束。' : '社团所属活动和相关报名也会删除。';
    return [
      button('恢复归档', confirmed(`确认恢复「${name}」？公开范围按所属社团的当前审核状态和设置生效。`,
        () => submit('restore'))),
      button('永久删除', confirmed(`确认永久删除「${name}」？${related}删除后无法恢复。`,
        () => submit('purge')), 'danger'),
    ];
  }), pagination(state.clubArchiveOffset, next, offset => { state.clubArchiveOffset = offset; void render(); }));
}

function photoReviewRow(row, location) {
  const path = '/campus/photos/review/' + row.id;
  const photo = element('img', '', 'photo-review-image');
  photo.alt = '校园地点投稿照片';
  photo.hidden = true;
  const feedback = element('p', '', 'feedback');
  feedback.setAttribute('role', 'status');
  const preview = button('查看照片', async () => {
    if (photo.hidden && !photo.hasAttribute('src')) {
      preview.disabled = true;
      try {
        const file = await api(path + '/image', { photo: true });
        if (!photo.isConnected) return;
        const url = URL.createObjectURL(file);
        for (const type of ['load', 'error']) {
          photo.addEventListener(type, () => URL.revokeObjectURL(url), { once: true });
        }
        photo.src = url;
        notice(feedback);
      } catch (error) {
        notice(feedback, error.message, true);
        return;
      } finally {
        preview.disabled = false;
      }
    }
    photo.hidden = !photo.hidden;
    preview.textContent = photo.hidden ? '查看照片' : '收起照片';
    preview.setAttribute('aria-expanded', String(!photo.hidden));
  });
  preview.setAttribute('aria-expanded', 'false');
  const review = status => api(path, { method: 'PATCH', data: { status, version: row.version } });
  const controls = group(preview);
  if (row.status !== 'approved') controls.append(button('通过审核',
    confirmed('通过后，校园地图的用户可以看到这张照片。确认发布？', () => review('approved')), 'primary'));
  if (row.status !== 'rejected') controls.append(button(row.status === 'approved' ? '撤下' : '驳回',
    confirmed(row.status === 'approved' ? '确认将这张照片移出公开相册？' : '确认驳回这张投稿？',
      () => review('rejected')), 'danger'));
  const unpublish = row.status === 'approved' ? '照片会从公开相册移除。' : '';
  controls.append(button('永久删除', confirmed(`确认永久删除「${location}」的这张照片？${unpublish}删除后无法恢复。`,
    () => api(path + '?version=' + row.version, { method: 'DELETE' }), '照片已永久删除。'), 'danger'));

  const time = element('time', new Date(row.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }), 'muted');
  time.dateTime = row.created_at;
  const metadata = element('div');
  metadata.append(group(element('span', reviewLabels[row.status], 'badge'), time),
    element('p', '投稿人 ID ' + row.uploader_id, 'muted'));
  const header = element('div', '', 'photo-review-row');
  header.append(metadata, controls);
  const entry = element('li');
  entry.append(header, photo, feedback);
  return entry;
}

function photoPlaceList(row, status, expanded) {
  const [lon, lat] = row.place.split('_').map(value => Number(value) / 10000);
  const coordinates = `${lat.toFixed(4)}°N，${lon.toFixed(4)}°E`;
  const location = row.name || '自选拍摄点';
  const details = element('details', '', 'card photo-place');
  details.setAttribute('name', 'photo-review-places');
  const summary = element('summary', location);
  summary.append(element('span', coordinates, 'muted'), element('span', `${row.photo_count} 张照片`, 'badge'));
  const body = element('div', '', 'photo-place-body');
  details.append(summary, body);
  let revision = 0;

  async function load(offset) {
    const current = ++revision;
    state.photoOffset = offset;
    body.replaceChildren(element('p', '正在读取照片…', 'muted'));
    const query = new URLSearchParams({ status, place: row.place, offset });
    try {
      const { items, next_offset: next } = await api('/campus/photos/review?' + query);
      if (current !== revision || !details.isConnected || !details.open) return;
      const list = element('ul', '', 'photo-review-list');
      list.append(...items.map(item => photoReviewRow(item, row.name || coordinates)));
      body.replaceChildren(items.length ? list : element('p', '这个地点当前没有符合筛选条件的照片。', 'muted'));
      if (offset || next != null) body.append(pagination(offset, next, load, photoReviewPageSize));
    } catch (error) {
      if (current === revision && details.isConnected && details.open) {
        body.replaceChildren(element('p', error.message, 'feedback error'));
      }
    }
  }

  details.addEventListener('toggle', () => {
    if (!details.isConnected || !details.open) return;
    const offset = state.photoPlace === row.place ? state.photoOffset : 0;
    const last = Math.floor((row.photo_count - 1) / photoReviewPageSize) * photoReviewPageSize;
    state.photoPlace = row.place;
    void load(Math.min(offset, last));
  });
  details.open = expanded;
  return details;
}

async function campusPhotos(target) {
  const status = state.photoStatus;
  const filter = select([['all', '全部状态'], ...Object.entries(reviewLabels)], status);
  filter.addEventListener('change', () => {
    state.photoStatus = filter.value;
    state.photoPlaceOffset = state.photoOffset = 0;
    void render();
  });
  target.append(card('投稿状态', label('筛选', filter)));
  const loadPlaces = offset => api('/campus/photos/places?' + new URLSearchParams({ status, offset }));
  let page = await loadPlaces(state.photoPlaceOffset);
  if (!target.isConnected) return;
  if (!page.items.length && state.photoPlaceOffset) {
    state.photoPlaceOffset = 0;
    page = await loadPlaces(0);
    if (!target.isConnected) return;
  }
  if (!page.items.length) {
    target.append(card('', element('p', '这个状态下没有照片。', 'muted')));
    return;
  }
  const selected = page.items.find(row => row.place === state.photoPlace) || page.items[0];
  target.append(...page.items.map(row => photoPlaceList(row, status, row === selected)));
  if (state.photoPlaceOffset || page.next_offset != null) {
    target.append(pagination(state.photoPlaceOffset, page.next_offset, offset => {
      state.photoPlaceOffset = offset;
      void render();
    }, photoReviewPageSize));
  }
}

async function campus(target) {
  const resources = [['classroom', '教室'], ['gym', '体育场馆'], ['club', '社团与活动'], ['library', '馆藏'],
    ['schedule', '模拟课表'], ['map', '校园街景']];
  const selector = select(resources, state.campus);
  selector.addEventListener('change', () => { state.campus = selector.value; void render(); });
  const controls = group(label('业务目录', selector));
  if (state.campus === 'schedule') {
    const week = select(Array.from({ length: 20 }, (_, index) => [index + 1, '第 ' + (index + 1) + ' 周']), state.week);
    week.addEventListener('change', () => { state.week = Number(week.value); void render(); });
    controls.append(label('周次', week));
  }
  const details = card('实时目录', controls);
  const access = card('开放范围', element('p', '黑名单优先。填写的各项白名单都须满足。按学院或年级限制时，用户资料须经 CAS 管理员核验。', 'muted'));
  const status = card('校园后端进程');
  const upstreams = card('MINTA 网关连接');
  target.append(status, upstreams, access, details);
  const service = state.campus;
  const path = '/campus/' + service + (service === 'schedule' ? '?week=' + state.week : '');
  const rulePath = '/campus/rules/' + service;
  const review = service === 'club' ? card('社团登记审核') : null;
  const archives = service === 'club' ? card('社团归档') : null;
  if (review) target.append(review, archives);
  const results = await Promise.allSettled([
    api('/runtime/processes'), api('/minta/readyz'), api(path), api(rulePath),
    review && clubReviewTable(review), archives && clubArchiveTable(archives),
  ]);
  if (results[0].status === 'fulfilled') status.append(processTable(results[0].value.items.filter(item => campusProcesses.includes(item.name))));
  else status.append(element('p', results[0].reason.message, 'feedback error'));
  if (results[1].status === 'fulfilled') upstreams.append(table([['name', '后端'],
    [item => item.connected ? '已连接' : '暂不可达', '连接状态']], results[1].value.items));
  else upstreams.append(element('p', results[1].reason.message, 'feedback error'));
  if (results[3].status === 'fulfilled') {
    const { rules, grades } = results[3].value;
    access.append(button('编辑服务黑白名单', () => editCampusRule(rulePath, '编辑服务开放范围', rules[''], grades, false)));
    if (service === 'classroom' && results[2].status === 'fulfilled') {
      details.append(table([['name', '教室'], ['id', '编号'], ['capacity', '容量'],
        [row => rules[row.id]?.blocked ? '手工占用' : row.reserved ? '已预约' : '空闲', '当前状态']],
      results[2].value.items, row => [button('编辑状态与开放范围', () => editCampusRule(
        rulePath + '/' + encodeURIComponent(row.id), row.name, rules[row.id], grades, true))]));
    }
  } else access.append(element('p', results[3].reason.message, 'feedback error'));
  if (results[2].status === 'fulfilled') {
    if (service !== 'classroom' || results[3].status !== 'fulfilled') details.append(output(results[2].value));
  }
  else details.append(element('p', results[2].reason.message, 'feedback error'));
  if (review && results[4].status === 'rejected') review.append(element('p', results[4].reason.message, 'feedback error'));
  if (archives && results[5].status === 'rejected') archives.append(element('p', results[5].reason.message, 'feedback error'));
}

function fileData(file) {
  if (file.size < 1 || file.size > 20 * 1024 * 1024) throw new Error('文件必须非空且不超过 20 MiB。');
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => resolve(reader.result.split(',', 2)[1]));
    reader.addEventListener('error', () => reject(new Error('无法读取文件。')));
    reader.readAsDataURL(file);
  });
}

function ragImportProgress() {
  const caption = element('span');
  const meter = element('progress');
  meter.max = 1;
  const node = element('label', '', 'rag-import-progress');
  node.hidden = true;
  node.append(caption, meter);
  function show(stage, fraction = null) {
    caption.textContent = fraction === null ? stage : `${stage} ${Math.floor(fraction * 100)}%`;
    if (fraction === null) meter.removeAttribute('value');
    else meter.value = fraction;
    node.hidden = false;
  }
  async function submit(data) {
    show('正在上传…', 0);
    try {
      return await api('/rag/documents', { method: 'POST', data, onUpload: fraction => {
        if (fraction === null) show('正在解析正文并写入 RAG…');
        else show('正在上传…', fraction);
      } });
    } finally {
      node.hidden = true;
    }
  }
  return { node, show, submit };
}

async function rag(target) {
  const scopes = await api('/rag/scopes').then(data => data.scopes || []).catch(() => []);
  const choices = select([...new Set([state.scope, ...scopes])].map(name => [name, name]), state.scope);
  choices.addEventListener('change', () => { state.scope = choices.value; void render(); });
  const scopeName = state.scope;
  const scope = input('text', scopeName, 64);
  const scopeQuery = '?scope=' + encodeURIComponent(scopeName);
  const importText = button('导入文本', () => {
    const progress = ragImportProgress();
    edit('导入到 ' + scopeName, [
      ['source', '资料来源', '', { max: 500 }], ['text', '正文', '', { multiline: true }],
    ], data => progress.submit({ scope: scopeName, ...data }));
    byId('editor-fields').append(progress.node);
  }, 'primary');
  const controls = group(label('已有资料范围', choices), label('资料范围', scope), button('切换范围', () => {
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(scope.value.trim())) return notice(byId('feedback'), '资料范围格式无效。', true);
    state.scope = scope.value.trim();
    void render();
  }), importText);
  const file = input('file');
  const progress = ragImportProgress();
  const importFile = button('导入文件', async () => {
    const selected = file.files[0];
    if (!selected) return notice(byId('feedback'), '请先选择文件。', true);
    importFile.disabled = importText.disabled = true;
    notice(byId('feedback'));
    try {
      progress.show('正在读取文件…');
      await action(async () => progress.submit({
        scope: scopeName, filename: selected.name, data_base64: await fileData(selected),
      }), '资料已写入。');
    } finally {
      progress.node.hidden = true;
      importFile.disabled = importText.disabled = false;
    }
  });
  controls.append(label('导入本地文件', file), importFile);
  const query = input('search', '', 4000);
  const preview = card('');
  preview.hidden = true;
  const previewContent = element('div');
  let previewVersion = 0;
  let previewTrigger;
  const close = button('×', () => {
    ++previewVersion;
    preview.hidden = true;
    previewContent.replaceChildren();
    previewTrigger?.focus({ preventScroll: true });
  }, 'rag-preview-close');
  close.setAttribute('aria-label', '关闭预览');
  const heading = element('div', '', 'rag-preview-heading');
  heading.append(element('h2', '查看或检索结果'), close);
  preview.append(heading, previewContent);

  async function showPreview(trigger, load) {
    const version = ++previewVersion;
    previewTrigger = trigger;
    preview.hidden = false;
    previewContent.replaceChildren(element('p', '正在读取…', 'muted'));
    try {
      const data = await load();
      if (version === previewVersion) previewContent.replaceChildren(output(data));
    } catch (error) {
      if (version === previewVersion) previewContent.replaceChildren(element('p', error.message, 'feedback error'));
    }
  }

  controls.append(label('检索关键词', query), button('检索', event => {
    void showPreview(event.currentTarget, () => api('/rag/search', { method: 'POST', data: { scope: scopeName, query: query.value } }));
  }));
  target.append(card('资料管理', controls, progress.node));
  const list = await api('/rag/documents' + scopeQuery);
  target.append(table([['source', '来源'], ['document_id', '文档 ID']], list.documents, row => {
    const path = '/rag/documents/' + row.document_id + scopeQuery;
    return [
      button('查看', event => void showPreview(event.currentTarget, async () => {
        const data = await api(path);
        return data.chunks.map(chunk => chunk.text).join('\n\n');
      })),
      button('删除', confirmed('删除当前资料范围中的这份文档？', () => api(path, { method: 'DELETE' })), 'danger'),
    ];
  }), preview);
}

async function capabilities(target) {
  const [skills, mcp] = await Promise.allSettled([api('/runtime/skills'), api('/runtime/tools')]);
  const section = card('子智能体与 SubSkill');
  const tools = card('MCP 工具');
  target.append(section, tools);
  if (skills.status === 'fulfilled') {
    const available = new Set(skills.value.minta_actions);
    section.append(table([
      ['name', '技能'], ['description', '用途'],
      [skill => skill.registered_agents.map(agent => agent.name + (agent.enabled ? ' · 已启用' : ' · 未启用')), '负责的子智能体'],
      [skill => skill.actions.map(item => item.label + (available.has(item.id) ? ' · 后端在线' : ' · 暂未发现后端动作')), '办理动作'],
    ], skills.value.skills));
  } else section.append(element('p', skills.reason.message, 'feedback error'));
  if (mcp.status === 'fulfilled') tools.append(table([
    ['name', '工具'], ['description', '用途'], [() => '已注册', '状态'], ['read_only', '只读'],
  ], mcp.value.items));
  else tools.append(element('p', mcp.reason.message, 'feedback error'));
}

function routingTags(ids, roles, empty) {
  const tags = element('div', '', 'routing-tags');
  for (const id of new Set(ids)) {
    tags.append(element('span', roles.get(id) || id, 'routing-tag routing-tag-' + id));
  }
  if (!tags.childElementCount) tags.append(element('span', empty, 'muted'));
  return tags;
}

function routingMetrics(totals, drillDown) {
  const metrics = element('div', '', 'routing-metrics');
  const percent = totals.reviewed ? new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })
    .format(totals.agreed / totals.reviewed) : '—';
  const values = [
    ['留存样本', routingNumber.format(totals.total), 'all'],
    ['待标注', routingNumber.format(totals.pending), 'pending'],
    ['已标注', routingNumber.format(totals.reviewed), 'reviewed'],
  ];
  for (const [labelText, value, status] of values) {
    const item = button('', () => drillDown({ status, agent: '', origin: '', day: '', q: '' }), 'routing-metric');
    item.append(element('span', labelText), element('strong', value));
    metrics.append(item);
  }
  const agreement = element('div', '', 'routing-metric');
  agreement.title = '已标注样本中，实际结果与标注目标一致的比例';
  agreement.append(element('span', '标注一致率'), element('strong', percent),
    element('small', totals.reviewed ? routingNumber.format(totals.agreed) + ' 条与标注一致' : '等待样本标注'));
  metrics.append(agreement);
  return metrics;
}

function routingLegend(first, second) {
  const legend = element('div', '', 'routing-legend');
  legend.append(element('span', first, 'routing-legend-primary'), element('span', second, 'routing-legend-secondary'));
  return legend;
}

function routingTimeline(timeline, totals, filters, drillDown) {
  const section = card('');
  const heading = element('div', '', 'routing-chart-heading');
  const period = select([[7, '近 7 天'], [14, '近 14 天'], [30, '近 30 天']], String(filters.days));
  period.setAttribute('aria-label', '采集趋势时间范围');
  period.addEventListener('change', () => {
    filters.days = Number(period.value);
    void render();
  });
  heading.append(element('h2', '采集趋势'), period);
  section.append(heading, routingLegend('已标注', '待标注'));
  const peak = Math.max(...timeline.map(day => day.total), 0);
  if (!peak) section.append(element('p', '这段时间还没有带采集日期的样本。', 'routing-chart-empty muted'));
  else {
    const chart = element('div', '', 'routing-timeline');
    for (const day of timeline) {
      const caption = day.day + ' · 已标注 ' + day.reviewed + ' 条，待标注 ' + (day.total - day.reviewed) + ' 条';
      const column = button('', () => drillDown({ status: 'all', agent: '', origin: '', day: day.day, q: '' }), 'routing-day');
      column.title = caption;
      column.setAttribute('aria-label', caption);
      column.disabled = day.total === 0;
      const bar = element('span', '', 'routing-day-bar');
      bar.style.height = (day.total / peak * 100) + '%';
      const reviewed = element('span', '', 'routing-day-reviewed');
      if (day.total) reviewed.style.height = (day.reviewed / day.total * 100) + '%';
      bar.append(reviewed);
      column.append(bar);
      chart.append(column);
    }
    const axis = element('div', '', 'routing-axis');
    for (const index of [0, Math.floor(timeline.length / 2), timeline.length - 1]) {
      axis.append(element('span', timeline[index].day.slice(5).replace('-', '/')));
    }
    section.append(chart, axis);
  }
  if (totals.undated) {
    section.append(element('p', routingNumber.format(totals.undated) + ' 条历史样本没有采集日期。', 'routing-note muted'));
  }
  return section;
}

function routingDistribution(distribution, roles, drillDown) {
  const section = card('路由结果分布', routingLegend('实际结果', '标注目标'));
  const peak = Math.max(...distribution.flatMap(item => [item.actual, item.expected]), 1);
  const grid = element('div', '', 'routing-distribution');
  for (const item of distribution) {
    const name = roles.get(item.agent) || '未派发';
    const row = element('div', '', 'routing-role-row');
    const filter = button(name, () => drillDown({
      status: 'all', origin: '', day: '', q: '', agent: item.agent || 'undispatched',
    }), 'routing-role-link');
    filter.title = '按实际结果筛选样本';
    const counts = element('div', '', 'routing-role-counts');
    for (const [key, labelText] of [['actual', '实际派发'], ['expected', '标注目标']]) {
      const bar = element('progress', '', 'routing-bar routing-bar-' + key);
      bar.max = peak;
      bar.value = item[key];
      bar.setAttribute('aria-label', name + ' ' + labelText);
      const line = element('div', '', 'routing-bar-line');
      line.append(bar, element('span', routingNumber.format(item[key])));
      counts.append(line);
    }
    row.append(filter, counts);
    grid.append(row);
  }
  section.append(grid, element('p', '同一样本涉及多个角色时，每个角色各计一次。', 'routing-note muted'));
  return section;
}

function routingPreview(roles, total) {
  const field = input('text', '', 800);
  field.required = true;
  field.placeholder = '输入要试算的问题';
  const submit = element('button', '试算', 'primary');
  submit.type = 'submit';
  const form = element('form', '', 'routing-preview-form');
  form.append(label('试算问题', field), submit);
  const result = element('div', '', 'routing-preview-result');
  result.setAttribute('role', 'status');
  result.hidden = true;
  const section = card('路由建议试算', element('p', '当前参与匹配的标注样本有 ' + routingNumber.format(total)
    + ' 条。MainAgent 会结合会话独立判断最终派发。', 'muted'), form, result);
  const methods = { exact: '文本匹配', similar: '相似文本', semantic: '语义向量' };
  let sequence = 0;
  async function calculate(prompt) {
    prompt = prompt.trim();
    if (!prompt) {
      field.focus();
      return;
    }
    field.value = prompt;
    const current = ++sequence;
    submit.disabled = true;
    result.hidden = false;
    result.replaceChildren(element('p', '正在匹配…', 'muted'));
    try {
      const advice = await api('/routing/preview', { method: 'POST', data: { prompt } });
      if (current !== sequence) return;
      if (advice.suggest_direct || advice.suggested_targets !== null) {
        const targets = advice.suggest_direct ? ['direct'] : advice.suggested_targets;
        result.replaceChildren(element('p', '匹配方式 · ' + methods[advice.method], 'muted'),
          routingTags(targets, roles, '建议先澄清'));
      } else {
        const message = !advice.enabled ? '反馈已暂停，启用后可试算。'
          : advice.method === 'conflict' ? '相近样本的标注有冲突，请检查标注目标。' : '现有标注没有形成明确建议。';
        result.replaceChildren(element('p', message, 'muted'));
      }
    } catch (error) {
      if (current === sequence) result.replaceChildren(element('p', error.message, 'feedback error'));
    } finally {
      if (current === sequence) submit.disabled = false;
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    void calculate(field.value);
  });
  return { section, calculate };
}

function routingSamples(roles, filters, version, preview) {
  const form = element('form', '', 'routing-filters');
  const controls = {
    status: select([['all', '全部'], ['pending', '待标注'], ['reviewed', '已标注'], ['different', '分派与标注不同']], filters.status),
    agent: select([['', '全部'], ...roles, ['undispatched', '未派发']], filters.agent),
    origin: select([['', '全部'], ['automatic', '自动选角'], ['preferred', '带用户偏好']], filters.origin),
    day: input('date', filters.day),
    q: input('search', filters.q, 160),
  };
  controls.q.placeholder = '脱敏问题或样本 ID';
  const search = element('button', '筛选', 'primary');
  search.type = 'submit';
  const clear = button('清除筛选', () => apply({ status: 'all', agent: '', origin: '', day: '', q: '' }));
  form.append(label('标注状态', controls.status), label('实际结果', controls.agent), label('路由来源', controls.origin),
    label('采集日期', controls.day), label('搜索', controls.q), group(search, clear));
  form.addEventListener('submit', event => {
    event.preventDefault();
    apply(Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value.trim()])));
  });
  const all = input('checkbox');
  all.className = 'routing-check';
  const selectPage = element('label', '', 'routing-select-page');
  selectPage.append(all, element('span', '全选本页'));
  const feedback = element('p', '', 'feedback');
  feedback.setAttribute('role', 'status');
  const rows = element('div', '', 'routing-sample-rows');
  const checkboxes = new Map();
  const remove = button('删除选中', () => {
    const ids = [...checkboxes].filter(([, checkbox]) => checkbox.checked).map(([id]) => id);
    if (!ids.length || !window.confirm('删除选中的 ' + ids.length + ' 条路由样本？')) return;
    remove.disabled = true;
    void action(() => api('/routing/samples/bulk-delete', { method: 'POST', data: { sample_ids: ids } }),
      data => '已删除 ' + data.deleted_count + ' 条样本。').finally(syncSelection);
  }, 'danger');
  const section = card('路由样本', form, group(selectPage, remove, feedback), rows);
  section.classList.add('routing-samples');
  let sequence = 0;

  function syncSelection() {
    const checked = [...checkboxes.values()].filter(checkbox => checkbox.checked).length;
    all.disabled = checkboxes.size === 0;
    all.checked = checked > 0 && checked === checkboxes.size;
    all.indeterminate = checked > 0 && !all.checked;
    remove.disabled = checked === 0;
    remove.textContent = checked ? '删除选中（' + checked + '）' : '删除选中';
  }
  all.addEventListener('change', () => {
    for (const checkbox of checkboxes.values()) checkbox.checked = all.checked;
    syncSelection();
  });

  function apply(changes) {
    Object.assign(filters, { offset: 0 }, changes);
    for (const [key, control] of Object.entries(controls)) control.value = filters[key];
    void load();
  }

  function grade(row) {
    const path = '/routing/samples/' + row.id + '/grade';
    const direct = row.score == null ? row.answered_direct : row.expected_direct;
    const targets = direct ? ['direct'] : row.expected_targets ?? row.called_agents;
    edit(row.score == null ? '标注路由样本' : '编辑路由标注', [
      ['score', '评分', String(row.score ?? 3), { options: [1, 2, 3, 4, 5].map(score => [score, score]) }],
      ['targets', '标注目标 · 留空表示先澄清', targets,
        { checklist: true, options: [...roles], exclusive: 'direct' }],
    ], data => api(path, { method: 'PUT', data: {
      score: Number(data.score),
      targets: data.targets.filter(id => id !== 'direct'),
      direct: data.targets.includes('direct'),
    } }));
    byId('editor-fields').prepend(element('p', row.prompt, 'routing-review-prompt'));
  }

  function show(page) {
    filters.offset = page.offset;
    const entries = table([
      [row => {
        const checkbox = input('checkbox');
        checkbox.className = 'routing-check';
        checkbox.setAttribute('aria-label', '选择样本 ' + row.prompt);
        checkbox.addEventListener('change', syncSelection);
        checkboxes.set(row.id, checkbox);
        return checkbox;
      }, '选择'],
      ['prompt', '脱敏问题'],
      [row => routingTags(row.answered_direct ? ['direct'] : row.called_agents, roles, '未派发'), '实际结果'],
      [row => row.score == null ? '待标注' : row.score + ' / 5', '评分'],
      [row => row.score == null ? '待标注'
        : routingTags(row.expected_direct ? ['direct'] : row.expected_targets ?? [], roles, '先澄清'), '标注目标'],
      [row => element('div', (row.created_at == null ? '历史样本' : routingDate.format(row.created_at * 1000))
        + '\n' + (row.explicit ? '带用户偏好' : '自动选角'), 'routing-sample-meta'), '采集'],
    ], page.samples, row => {
      const path = '/routing/samples/' + row.id;
      const options = [
        button(row.score == null ? '标注' : '编辑', () => grade(row)),
        button('试算', () => {
          preview.section.scrollIntoView({ block: 'nearest' });
          void preview.calculate(row.prompt);
        }),
      ];
      if (row.score != null) options.push(button('清除标注', confirmed('清除标注并将样本移回待标注？',
        () => api(path + '/grade', { method: 'DELETE' }), '已清除标注。')));
      options.push(button('删除', confirmed('删除这条路由样本？', () => api(path, { method: 'DELETE' }), '已删除样本。'), 'danger'));
      return options;
    });
    entries.classList.add('routing-samples-table');
    rows.replaceChildren(entries);
    if (page.offset || page.next_offset != null) {
      rows.append(pagination(page.offset, page.next_offset, offset => {
        filters.offset = offset;
        void load();
      }, routingPageSize));
    }
    const range = page.samples.length ? ' · 本页 ' + (page.offset + 1) + '–' + (page.offset + page.samples.length) + ' 条' : '';
    notice(feedback, '共 ' + routingNumber.format(page.total) + ' 条' + range);
    syncSelection();
  }

  async function load() {
    const current = ++sequence;
    checkboxes.clear();
    syncSelection();
    rows.replaceChildren();
    notice(feedback, '正在读取样本…');
    const params = new URLSearchParams({ limit: routingPageSize, offset: filters.offset });
    for (const key of Object.keys(controls)) if (filters[key]) params.set(key, filters[key]);
    try {
      const page = await api('/routing/samples?' + params);
      if (current === sequence && version === state.version) show(page);
    } catch (error) {
      if (current === sequence && version === state.version) notice(feedback, error.message, true);
    }
  }
  return { section, apply, load };
}

async function routing(target, version) {
  const filters = state.routingFilters;
  const data = await api('/routing/overview?days=' + filters.days);
  if (version !== state.version) return;
  const roles = new Map([...data.roles.map(role => [role.id, role.name]), ['direct', routingDirectLabel]]);
  const preview = routingPreview(roles, data.totals.active_examples);
  const samples = routingSamples(roles, filters, version, preview);
  const drillDown = changes => {
    samples.apply(changes);
    samples.section.scrollIntoView({ block: 'start' });
  };
  const control = card('');
  control.classList.add('routing-feedback-control');
  const status = element('div');
  status.append(element('h2', data.enabled ? '反馈已启用' : '反馈已暂停'),
    element('p', '启用时采集脱敏路由样本，已标注样本为相近问题提供建议。'));
  const toggle = button(data.enabled ? '暂停反馈' : '启用反馈', () => {
    toggle.disabled = true;
    void action(() => api('/routing/status', { method: 'PUT', data: { enabled: !data.enabled } }),
      data.enabled ? '路由反馈已暂停。' : '路由反馈已启用。').finally(() => { toggle.disabled = false; });
  });
  control.append(status, toggle);
  const charts = element('div', '', 'routing-charts');
  charts.append(routingTimeline(data.timeline, data.totals, filters, drillDown),
    routingDistribution(data.distribution, roles, drillDown));
  const introductions = card('子智能体介绍', table([
    ['name', '子智能体'], ['introduction', '路由介绍'], [row => row.overridden ? '自定义' : '默认', '来源'],
  ], data.roles, row => {
    const path = '/routing/introductions/' + row.id;
    const options = [button('修改', () => edit('修改' + row.name + '介绍', [
      ['text', '介绍', row.introduction, { max: 1200, multiline: true }],
    ], data => api(path, { method: 'PUT', data })))];
    if (row.overridden) options.push(button('恢复默认', confirmed('将' + row.name + '的介绍恢复默认？',
      () => api(path, { method: 'DELETE' }), '已恢复默认介绍。')));
    return options;
  }));
  target.append(control, routingMetrics(data.totals, drillDown), charts, preview.section, samples.section, introductions);
  await samples.load();
}

async function campusProfileReview(target) {
  const lookup = input('search', state.campusProfileSearch, 160);
  const change = offset => { state.campusProfileOffset = offset; void render(); };
  const search = () => { state.campusProfileSearch = lookup.value.trim(); change(0); };
  lookup.addEventListener('keydown', event => { if (event.key === 'Enter') search(); });
  const controls = group(label('姓名、邮箱或学号', lookup), button('查询', search));
  target.append(controls);
  const parameters = new URLSearchParams({ q: state.campusProfileSearch, offset: state.campusProfileOffset });
  const { items, grades, next_offset: next } = await api('/cas/profiles?' + parameters);
  const pending = items.filter(row => row.batch_reviewable).map(({ id, revision }) => ({ id, revision }));
  const feedback = element('p', '', 'feedback');
  feedback.setAttribute('role', 'status');
  const approvePage = button(`一键核验本页（${pending.length}）`, async () => {
    if (!window.confirm(`确认本页 ${pending.length} 份待核验资料均已与校园名册核对，并可用于校务准入？`)) return;
    approvePage.disabled = true;
    notice(feedback, '正在核验本页资料…');
    const version = state.version;
    try {
      const result = await api('/cas/profiles/bulk-review', { method: 'POST', data: { items: pending } });
      if (version !== state.version) return;
      state.campusProfileOffset = 0;
      await render();
      if (version + 1 === state.version) notice(byId('feedback'), `已核验 ${result.approved_count} 份校园资料。`);
    } catch (error) {
      notice(feedback, error.message, true);
    } finally {
      approvePage.disabled = false;
    }
  }, 'primary');
  approvePage.disabled = !pending.length;
  controls.append(approvePage);
  target.append(feedback);
  target.append(table([[row => row.verified_campus_number || row.username || row.protocol.toUpperCase(), '校园登录标识'], ['email', '绑定邮箱'],
    ['full_name', '姓名'], ['campus_number', '登记学工号'], ['college', '学院'], [row => grades[row.grade], '年级'],
    [row => !row.revision ? '待登记' : row.campus_number_current === false ? '学工号待确认' : row.reviewed_at ? '已核验' : '待核验', '资料状态']], items, row => {
    const path = '/cas/profiles/' + row.id;
    const approved = Boolean(row.reviewed_at);
    const buttons = [];
    if (row.revision && row.is_active && row.campus_number_current !== false) buttons.push(button(approved ? '撤销核验' : '核验通过', confirmed(
      approved ? '撤销后，此资料将暂时无法用于受限校务。确认撤销？' : '请与校园名册核对。确认资料正确并用于校务准入？',
      () => api(path, { method: 'PATCH', data: { revision: row.revision, approved: !approved } }))));
    buttons.push(button('解除绑定', confirmed('解除校园身份会移除登记资料并结束该账号的现有登录。确认解除？',
      () => api(path, { method: 'DELETE' })), 'danger'));
    return buttons;
  }), pagination(state.campusProfileOffset, next, change));
}

function credentials(target) {
  const email = input('email', '', 254);
  const result = card('凭据详情');
  const getEmail = () => {
    if (!email.value || !email.checkValidity()) throw new Error('请填写有效的教师邮箱。');
    return email.value.trim();
  };
  const change = (operation, data) => api('/credentials/' + operation, { method: 'POST', data });
  const show = data => result.replaceChildren(element('h2', '凭据详情'), output(data));
  const changeContact = operation => {
    let address;
    try { address = getEmail(); } catch (error) { return notice(byId('feedback'), error.message, true); }
    edit(operation === 'issue' ? '签发教师凭据' : '修改教师凭据', [
      ['name', '教师姓名', '', { max: 120 }], ['phone', '联系电话', '', { type: 'tel', max: 40 }],
      ['days', '有效天数（1–730）', '365', { type: 'number' }],
    ], async data => {
      await change(operation, { email: address, contact: { name: data.name, phone: data.phone }, days: Number(data.days) });
    });
  };
  target.append(card('教师凭据管理', group(label('教师邮箱', email),
    button('查询', () => void action(async () => show(await change('inspect', { email: getEmail() })), '已读取凭据。', false)),
    button('签发', () => changeContact('issue'), 'primary'),
    button('修改', () => changeContact('update')),
    button('撤销', confirmed('撤销该教师的电子凭据？',
      async () => show(await change('revoke', { email: getEmail() })), '已撤销。', false), 'danger'),
  )), result);
}

async function audit(target) {
  const { items } = await api('/audit');
  const time = row => new Date(row.time * 1000).toLocaleString();
  const outcome = row => ({ completed: '完成', accepted: '已受理', pending: '等待确认', denied: '拒绝' })[row.outcome] || row.outcome;
  target.append(table([[time, '时间'], ['actor', '操作者'], ['action', '动作'], ['target', '目标'], [outcome, '结果']], items));
}

function selectView(view) {
  if (!Object.hasOwn(views, view) || view === state.view) return;
  state.view = view;
  adminContent.scrollTop = 0;
  void render();
}

const sections = byId('sections');
byId('rumor-login-details').addEventListener('click', () => selectView('rumor'));
for (const control of sections.querySelectorAll('button')) {
  sectionPicker.append(new Option(control.textContent, control.dataset.view));
}
sectionPicker.addEventListener('change', () => selectView(sectionPicker.value));
sections.addEventListener('click', event => selectView(event.target.closest('button')?.dataset.view));
byId('refresh').addEventListener('click', () => void render());
byId('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter || form.querySelector('button[type=submit]');
  submit.disabled = true;
  notice(byId('login-feedback'), '正在验证…');
  try {
    const session = await api('/login', { method: 'POST', data: Object.fromEntries(new FormData(form)) });
    form.reset();
    setSession(session);
    byId('view-title').focus({ preventScroll: true });
  } catch (error) {
    form.elements.password.value = '';
    notice(byId('login-feedback'), error.message, true);
  } finally {
    submit.disabled = false;
  }
});
byId('logout').addEventListener('click', async () => {
  try {
    await api('/logout', { method: 'POST' });
    setSession(null, '已退出管理台。');
  } catch (error) {
    notice(byId('feedback'), error.message, true);
  }
});

byId('weread-status').addEventListener('click', () => openWeReadDialog());
byId('weread-start').addEventListener('click', () => void refreshWeRead(true));
wereadDialog.addEventListener('close', () => {
  if (wereadDialog.open) return;
  setWeReadImage('');
  if (state.session && !weread.starting) void refreshWeRead();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    void refreshWeRead();
    void refreshRumorStatus();
  }
});

api('/session').then(setSession).catch(() => {});
