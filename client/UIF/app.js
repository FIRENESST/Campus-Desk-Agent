import { createCampusPanels } from './panels.js';
import { bindCampusAvatar } from './campus-avatar.js';
import { bindChatCompanion } from './chat-companion.js';

const byId = id => document.getElementById(id);
const themeSelect = byId('theme-select');
const customThemeEditor = byId('custom-theme');
const colorInputs = [...customThemeEditor.querySelectorAll('input[type="color"]')];
const workspaceBackground = byId('workspace-background');
const imagePicker = byId('workspace-image-file');
const removeImage = byId('remove-workspace-image');
const opacityInput = byId('background-opacity');
const landing = byId('landing');
const workspaceLayout = byId('workspace-layout');
const workspace = workspaceLayout.querySelector('main');
const workspaceEmpty = byId('workspace-empty');
const campusAvatar = byId('campus-avatar');
const avatarPreference = byId('show-campus-avatar');
const avatarStorageKey = 'campus-desk.show-live2d';
const viewport = byId('viewport');
const turnNav = byId('turn-nav');
const turnTicks = byId('turn-ticks');
const turnNavOlder = byId('turn-nav-older');
const turnPreview = byId('turn-preview');
const olderMessages = byId('older-messages');
const conversationSidebar = byId('sidebar');
const campusAppsToggle = byId('campus-apps-toggle');
const campusAppsDrawer = byId('campus-apps-drawer');
const campusAppsQuery = byId('campus-apps-query');
const campusAppButtons = [...campusAppsDrawer.querySelectorAll('[data-campus-panel]')];
const campusAppTerms = {
  schedule: '课表 上课 任课 授课', 'calendar-sync': '安卓 windows 同步 订阅 to do ics',
  'semester-report': '成绩 学分 分数', activities: '报名 讲座', wechat: '文章 校园日报 微信',
  rumor: '线索 核实', map: '路线 街景 照片 3d', classrooms: '教室 空闲',
  gym: '场馆 篮球 羽毛球 健身', library: '借书 还书 馆藏', clubs: '社团 组织 活动',
};
const compactCampusApps = window.matchMedia('(max-width: 960px)');
const welcomeHero = byId('welcome-hero');
const heroHoverMotion = window.matchMedia('(any-hover: hover) and (prefers-reduced-motion: no-preference)');
const themeStorageKey = 'campus-assistant-theme';
const availableThemes = new Set(Array.from(themeSelect.options, option => option.value));
const defaultCustomColors = Object.freeze(Object.fromEntries(colorInputs.map(input => [input.name, input.defaultValue])));
let themePreference;
let activeCustomPalette = {};
let heroThemeColor;

function colorChannels(hex) {
  return [1, 3, 5].map(index => Number.parseInt(hex.slice(index, index + 2), 16));
}

function relativeLuminance(rgb) {
  const [red, green, blue] = rgb.map(channel => {
    const srgb = channel / 255;
    return srgb <= .04045 ? srgb / 12.92 : ((srgb + .055) / 1.055) ** 2.4;
  });
  return .2126 * red + .7152 * green + .0722 * blue;
}

function mixColor(color, target, amount) {
  const targetChannels = colorChannels(target);
  const channels = colorChannels(color).map((channel, index) =>
    Math.round(channel + (targetChannels[index] - channel) * amount).toString(16).padStart(2, '0'));
  return '#' + channels.join('');
}

function fitTone(color, target, limit, lighten = false) {
  const fits = candidate => {
    const luminance = relativeLuminance(colorChannels(candidate));
    return lighten ? luminance >= limit : luminance <= limit;
  };
  if (fits(color)) return color;
  let lower = 0;
  let upper = 1;
  for (let step = 0; step < 8; step += 1) {
    const midpoint = (lower + upper) / 2;
    if (fits(mixColor(color, target, midpoint))) upper = midpoint;
    else lower = midpoint;
  }
  return mixColor(color, target, upper);
}

function artRotation(color) {
  const [red, green, blue] = colorChannels(color);
  const highest = Math.max(red, green, blue);
  const span = highest - Math.min(red, green, blue);
  if (!span) return '0deg';
  const sector = highest === red ? (green - blue) / span
    : highest === green ? 2 + (blue - red) / span : 4 + (red - green) / span;
  return `${Math.round(sector * 60 - 150)}deg`;
}

function customPalette(colors) {
  const light = '#ffffff';
  const dark = '#10201b';
  const accent = fitTone(colors.accent, dark, .135);
  const paper = fitTone(colors.paper, light, .84, true);
  const rail = fitTone(colors.rail, dark, .045);
  return {
    '--paper': paper,
    '--ink': mixColor(rail, '#172128', .70),
    '--muted': mixColor(accent, '#4a5855', .78),
    '--line': mixColor(paper, accent, .15),
    '--accent': accent,
    '--accent-strong': mixColor(accent, dark, .24),
    '--accent-light': mixColor(accent, light, .81),
    '--accent-tint': mixColor(paper, accent, .09),
    '--accent-border': mixColor(paper, accent, .26),
    '--rail': rail,
    '--rail-glow': mixColor(rail, accent, .22),
    '--rail-muted': mixColor(rail, light, .72),
    '--hero-tone': mixColor(rail, accent, .17),
    '--art-hue': artRotation(colors.accent),
  };
}

function loadTheme() {
  let stored;
  let saved;
  try {
    stored = localStorage.getItem(themeStorageKey);
    saved = JSON.parse(stored);
  } catch {}
  const name = availableThemes.has(stored) ? stored : saved?.name;
  const colors = Object.fromEntries(Object.entries(defaultCustomColors).map(([key, fallback]) => {
    const value = saved?.colors?.[key];
    return [key, typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : fallback];
  }));
  const opacity = saved?.backgroundOpacity;
  const backgroundOpacity = Number.isInteger(opacity) && opacity >= 0 && opacity <= 100
    ? opacity : Number(opacityInput.defaultValue);
  return { name: availableThemes.has(name) ? name : themeSelect.options[0].value, colors, backgroundOpacity };
}

function renderWorkspaceBackground() {
  const hasImage = themePreference.name === 'custom' && !workspaceBackground.hidden;
  const opacity = themePreference.backgroundOpacity;
  workspaceBackground.style.opacity = String(opacity / 100);
  workspaceBackground.parentElement.classList.toggle('has-background', hasImage && opacity > 0);
  opacityInput.value = String(opacity);
  opacityInput.disabled = !hasImage;
  byId('background-opacity-value').value = `${opacity}%`;
  removeImage.disabled = !hasImage || imagePicker.disabled;
}

function applyTheme(preference) {
  themePreference = preference;
  const root = document.documentElement;
  const isCustom = preference.name === 'custom';
  root.dataset.theme = preference.name;
  themeSelect.value = preference.name;
  customThemeEditor.hidden = !isCustom;
  for (const input of colorInputs) {
    if (input.value !== preference.colors[input.name]) input.value = preference.colors[input.name];
  }
  const palette = isCustom ? customPalette(preference.colors) : {};
  for (const key of Object.keys(activeCustomPalette)) {
    if (!(key in palette)) root.style.removeProperty(key);
  }
  for (const [key, value] of Object.entries(palette)) root.style.setProperty(key, value);
  activeCustomPalette = palette;
  const colors = getComputedStyle(root);
  heroThemeColor = colorChannels(colors.getPropertyValue('--hero-tone').trim() || '#17382e');
  document.querySelector('meta[name="theme-color"]').content = colors.getPropertyValue('--rail').trim();
  renderWorkspaceBackground();
  updateHeroScroll();
}

function persistTheme() {
  try {
    localStorage.setItem(themeStorageKey, JSON.stringify(themePreference));
    feedback('theme-storage-feedback');
  } catch {
    feedback('theme-storage-feedback', '配色已在当前页面应用。浏览器存储暂不可用。', true);
  }
}

function saveTheme(preference) {
  applyTheme(preference);
  persistTheme();
}

async function workspaceImageStore(mode, operation) {
  const database = await new Promise((resolve, reject) => {
    const open = indexedDB.open('campus-assistant-appearance', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('images');
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction('images', mode);
      const request = operation(transaction.objectStore('images'));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    database.close();
  }
}

async function drawWorkspaceImage(file) {
  const context = workspaceBackground.getContext('2d');
  const bitmap = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 2048 / Math.max(bitmap.width, bitmap.height));
    workspaceBackground.width = Math.max(1, Math.round(bitmap.width * scale));
    workspaceBackground.height = Math.max(1, Math.round(bitmap.height * scale));
    context.drawImage(bitmap, 0, 0, workspaceBackground.width, workspaceBackground.height);
    workspaceBackground.hidden = false;
  } finally {
    bitmap.close();
  }
}

async function restoreWorkspaceImage() {
  try {
    const image = await workspaceImageStore('readonly', images => images.get('workspace'));
    if (image) await drawWorkspaceImage(image);
  } catch {
    // The workspace can use its theme background when browser storage is unavailable.
  } finally {
    imagePicker.disabled = false;
    renderWorkspaceBackground();
  }
}

const backgroundImageTypes = new Set(['image/png', 'image/jpeg', 'image/webp']);

async function setWorkspaceImage(file) {
  if (themePreference.name !== 'custom' || imagePicker.disabled) return;
  if (file && (!backgroundImageTypes.has(file.type) || file.size === 0 || file.size > 10 * 1024 ** 2)) {
    feedback('workspace-image-feedback', '请选择 10 MB 以内的 PNG、JPG 或 WebP 图片。', true);
    return;
  }
  imagePicker.disabled = true;
  renderWorkspaceBackground();
  feedback('workspace-image-feedback');
  try {
    if (file) {
      await drawWorkspaceImage(file);
    } else {
      workspaceBackground.hidden = true;
      workspaceBackground.width = workspaceBackground.height = 1;
    }
    renderWorkspaceBackground();
    try {
      await workspaceImageStore('readwrite', images =>
        file ? images.put(file.slice(0, file.size, file.type), 'workspace') : images.delete('workspace'));
      if (file) feedback('workspace-image-feedback', '背景图已保存到当前浏览器。');
    } catch {
      feedback('workspace-image-feedback', '背景图已在当前页面更新。浏览器存储暂不可用。', true);
    }
  } catch {
    feedback('workspace-image-feedback', '无法读取这张图片，请换一张重试。', true);
  } finally {
    imagePicker.disabled = false;
    renderWorkspaceBackground();
  }
}

applyTheme(loadTheme());
themeSelect.addEventListener('change', () => {
  saveTheme({ ...themePreference, name: themeSelect.value });
});
for (const input of colorInputs) {
  const updateColor = () => {
    if (themePreference.name !== 'custom' || input.value === themePreference.colors[input.name]) return;
    saveTheme({ ...themePreference, colors: { ...themePreference.colors, [input.name]: input.value } });
  };
  input.addEventListener('input', updateColor);
  input.addEventListener('change', updateColor);
}
byId('reset-custom-theme').addEventListener('click', () => {
  saveTheme({ ...themePreference, name: 'custom', colors: { ...defaultCustomColors } });
});
imagePicker.addEventListener('change', () => {
  const [file] = imagePicker.files;
  imagePicker.value = '';
  if (file) void setWorkspaceImage(file);
});
removeImage.addEventListener('click', () => void setWorkspaceImage(null));
opacityInput.addEventListener('input', () => {
  if (opacityInput.disabled) return;
  themePreference = { ...themePreference, backgroundOpacity: Number(opacityInput.value) };
  renderWorkspaceBackground();
  persistTheme();
});
window.addEventListener('storage', event => {
  if (event.key === themeStorageKey || event.key === null) applyTheme(loadTheme());
});
void restoreWorkspaceImage();

// Older website processes can still display plain text until they serve the new assets.
const { renderMarkdown } = await import('./markdown.mjs').catch(() => ({}));

const query = byId('query');
const updateChatCompanion = bindChatCompanion(byId('chat-companion-dock'), query, () => state.user);
const target = byId('target');
const subagentNames = new Map(Array.from(target.options, option => [option.value, option.label]));
const authDialog = byId('auth-dialog');
const instanceDialog = byId('instance-dialog');
const settingsDialog = byId('settings-dialog');
const passwordDialog = byId('password-dialog');
const profileDialog = byId('profile-dialog');
const conversationDialog = byId('conversation-dialog');
const deleteDialog = byId('delete-dialog');
const historyQuery = byId('history-query');
const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const activeRuns = new Set(['queued', 'running']);
const authCopy = {
  login: ['登录校园助手', '邮箱账号须绑定校园统一身份并登记资料。已绑定的用户也可以直接使用校园统一认证。'],
  register: ['创建账号', '支持 QQ、网易、Gmail 和 Outlook 邮箱。注册后使用邮件中的令牌验证邮箱。'],
  verify: ['验证邮箱', '粘贴验证邮件中的一次性令牌，有效期 10 分钟。'],
  'verification-request': ['重发验证邮件', '输入注册邮箱。每次发送后需等 60 秒。'],
  'password-request': ['找回密码', '填写邮箱，申请一次性密码重置令牌。'],
  reset: ['设置新密码', '粘贴重置邮件中的令牌，然后设置新密码。'],
};
const resultCopy = {
  partial: '已返回部分结果',
  needs_configuration: '本次请求涉及尚未配置的校园服务',
  needs_input: '需要补充信息',
  blocked: '本次请求未能继续',
  failed: '本次对话运行失败',
  uncertain: '本次运行未能确认完成',
};
const timeFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' });

function newView(record = null) {
  return { record, messages: [], turns: [], cursor: null, loaded: !record, runId: record?.active_run_id || null, status: null };
}

const state = {
  user: null, generation: 0, instances: [], space: null, view: newView(),
  campus: false, pending: new Map(), authMode: 'login', authBusy: false, instancesLoaded: false, instanceRequest: 0,
  watches: new Map(), withdrawnRuns: new Set(),
};
let instanceEditor = null;
let loginShortcut = null;
let conversationEditor = null;
let deletion = null;
let toastTimer = 0;
let verificationEmail = '';
let resetEmail = '';
let passwordOperation = null;
let passwordDialogGeneration = null;
let mailTimer = 0;
const mailDeadlines = new Map();
const resendSeconds = 60;
const campusPanels = createCampusPanels(api, () => state.user, (claim, publishedOn) => {
  setCampusApps(false);
  query.value = claim ? `请帮我核实这条校园消息是否属实：${claim}${publishedOn ? `\n原消息发布于 ${publishedOn}` : ''}`
    : '请帮我核实这条校园消息是否属实：';
  query.dispatchEvent(new Event('input'));
  query.focus();
});

class ApiError extends Error {
  constructor(message, status, code, retryAfter = 0) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

async function api(path, { method = 'GET', data, upload, key, guest = false, signal, events = false, calendar = false } = {}) {
  const generation = state.generation;
  const headers = {};
  if (upload !== undefined) headers['Content-Type'] = 'application/octet-stream';
  else if (data !== undefined) headers['Content-Type'] = 'application/json';
  if (events) headers.Accept = 'text/event-stream';
  if (calendar) headers.Accept = 'text/calendar';
  if (key) headers['Idempotency-Key'] = key;
  if (!guest && state.user?.websiteContext) headers['X-Website-Context'] = state.user.websiteContext;
  let reply;
  try {
    reply = await fetch(path, { method, headers, signal, body: upload ?? (data === undefined ? undefined : JSON.stringify(data)) });
  } catch {
    throw new ApiError('网络连接暂不可用，请重试。', 0);
  }
  if (reply.ok && events) {
    if (reply.headers.get('Content-Type')?.split(';')[0] === 'text/event-stream' && reply.body) return reply.body;
    await reply.body?.cancel();
    throw new ApiError('校园服务返回了无法读取的更新。', 502);
  }
  if (reply.ok && calendar) {
    if (reply.headers.get('Content-Type')?.split(';')[0] === 'text/calendar') return reply.blob();
    throw new ApiError('校园服务返回了无法读取的日历。', 502);
  }
  if (reply.status === 204) return null;
  const payload = await reply.json().catch(() => null);
  if (!reply.ok) {
    if (reply.status === 401 && !guest && !signal?.aborted && generation === state.generation && state.user) {
      replaceUser(null);
      if (payload?.error?.code === 'website_context_changed') {
        showNotice(payload.error.message, { label: '重新载入', run: () => { showNotice(); void initialize(); } });
      } else {
        showNotice('登录已过期，请重新登录。', { label: '登录', run: () => openAuth('login') });
      }
    }
    if (!guest && state.user?.campus_status === 'ready' && generation === state.generation &&
        ['campus_link_required', 'campus_profile_required'].includes(payload?.error?.code)) {
      void initialize();
    }
    throw new ApiError(payload?.error?.message || '请求未完成，请稍后重试。', reply.status, payload?.error?.code,
      Number(reply.headers.get('Retry-After')));
  }
  if (!payload) throw new ApiError('校园服务返回了无法读取的结果。', 502);
  if (path === '/v1/me') payload.websiteContext = reply.headers.get('X-Website-Context');
  return payload;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const use = document.createElementNS(ns, 'use');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function feedback(id, message = '', error = false) {
  const node = byId(id);
  node.hidden = !message;
  node.textContent = message;
  node.dataset.tone = error ? 'error' : 'info';
}

function showNotice(message = '', action) {
  byId('notice').hidden = !message;
  byId('notice-text').textContent = message;
  const button = byId('notice-action');
  button.hidden = !action;
  button.textContent = action?.label || '';
  button.onclick = action?.run || null;
}

function toast(message = '') {
  clearTimeout(toastTimer);
  byId('action-toast-text').textContent = message;
  byId('action-toast').hidden = !message;
  if (message) toastTimer = setTimeout(() => toast(), 3200);
}

function sidebar(open) {
  conversationSidebar.classList.toggle('is-open', open);
  byId('close-sidebar').hidden = !open;
  byId('open-sidebar').setAttribute('aria-expanded', String(open));
}

function filterCampusApps() {
  const words = campusAppsQuery.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = campusAppButtons.filter(button => {
    const searchable = `${button.textContent} ${campusAppTerms[button.dataset.campusPanel] || ''}`.toLowerCase();
    const visible = words.every(word => searchable.includes(word));
    button.hidden = !visible;
    return visible;
  });
  byId('campus-apps-empty').hidden = matches.length > 0;
  return matches;
}

function setCampusApps(open, restoreFocus = false) {
  const overlay = open && compactCampusApps.matches;
  if (overlay) {
    sidebar(false);
    campusPanels.close();
  }
  const moveFocus = !open && (restoreFocus || campusAppsDrawer.contains(document.activeElement));
  campusAppsToggle.setAttribute('aria-expanded', String(open));
  campusAppsDrawer.inert = !open;
  workspace.inert = overlay;
  conversationSidebar.inert = overlay;
  if (!open && campusAppsQuery.value) {
    campusAppsQuery.value = '';
    filterCampusApps();
  }
  if (moveFocus) campusAppsToggle.focus({ preventScroll: true });
}

function openCampusApp(button) {
  if (compactCampusApps.matches) setCampusApps(false);
  if (!state.user) {
    loginShortcut = button;
    openAuth('login', '登录后即可使用该校园功能。');
    return;
  }
  campusPanels.open(button.dataset.campusPanel, () => {
    const trigger = campusAppsDrawer.inert ? campusAppsToggle : button;
    trigger.focus({ preventScroll: true });
  });
}

function replaceUser(user, preserveDraft = false) {
  hideAvatarGreeting();
  state.generation += 1;
  state.instanceRequest += 1;
  state.user = user;
  state.instances = [];
  state.instancesLoaded = false;
  stopHistory(state.space);
  state.space = null;
  historyQuery.value = '';
  state.pending.clear();
  for (const watch of state.watches.values()) watch.controller.abort();
  state.watches.clear();
  state.withdrawnRuns.clear();
  instanceEditor = conversationEditor = deletion = null;
  passwordOperation = null;
  toast();
  campusPanels.close();
  for (const dialog of [profileDialog, passwordDialog, authDialog, settingsDialog, instanceDialog, conversationDialog, deleteDialog]) {
    if (dialog.open) dialog.close();
  }
  showView(newView(), preserveDraft);
  renderAccount();
  renderInstances();
  renderMailButtons();
}

function accountName(user) {
  return user?.display_name?.trim() || user?.full_name?.trim() || '';
}

function renderAccount() {
  byId('side-settings-hint').textContent = accountName(state.user) || state.user?.email || '外观与账号';
  byId('campus-login').hidden = !state.campus;
  renderGreeting();
}

function renderGreeting() {
  const hour = new Date().getHours();
  let salutation = '晚上好';
  if (hour >= 5 && hour < 11) salutation = '早上好';
  else if (hour >= 11 && hour < 14) salutation = '中午好';
  else if (hour >= 14 && hour < 18) salutation = '下午好';
  const user = state.user;
  let recipient = '访客';
  if (user) {
    const title = user.role === 'teacher' ? (user.academic_title?.trim() || '老师') : '同学';
    const name = accountName(user);
    recipient = name.endsWith(title) ? name : `${name}${title}`;
  }
  byId('topbar-greeting').textContent = `${salutation}，${recipient}`;
}

function renderInstances() {
  const select = byId('instance-select');
  select.disabled = !state.user || !state.instances.length;
  byId('add-instance').disabled = !state.user;
  byId('edit-instance').disabled = !state.instances.length;
  select.replaceChildren(...state.instances.map(instance => {
    const tags = [];
    if (instance.is_default && instance.name !== '默认实例') tags.push('默认');
    if (instance.status === 'paused') tags.push('已暂停');
    return new Option(instance.name + (tags.length ? `（${tags.join(' · ')}）` : ''), instance.id);
  }));
  if (state.space) select.value = state.space.instance.id;
  if (!state.instances.length) {
    const empty = !state.user ? '登录后可选择' : state.instancesLoaded ? '暂无实例' : '正在读取实例…';
    select.add(new Option(empty, ''));
  }
  const instance = state.space?.instance;
  const paused = instance?.status === 'paused';
  const context = byId('workspace-context');
  context.hidden = !instance;
  context.dataset.paused = String(paused);
  byId('workspace-context-label').textContent = instance ? (paused ? `已暂停 · ${instance.name}` : instance.name) : '';
  if (instanceDialog.open && instanceDialog.dataset.mode === 'manage') renderInstanceDirectory();
}

function currentChatView(conversationId) {
  return state.view.record?.id === conversationId ? state.view : null;
}

function chatIsProcessing(chat) {
  return Boolean(state.watches.has(chat.id) || chat.active_run_id || state.pending.get(chat.id)?.sending);
}

function setChatActivity(conversationId, runId, finishedId = null) {
  const saved = state.space?.chats.find(item => item.id === conversationId);
  const open = currentChatView(conversationId)?.record;
  for (const record of [saved, open]) {
    if (record && runId !== undefined && (!finishedId || record.active_run_id === finishedId)) record.active_run_id = runId;
  }
  const row = byId('history-list').querySelector(`[data-chat-id="${CSS.escape(conversationId)}"]`);
  if (row && saved) row.dataset.running = String(chatIsProcessing(saved));
}

function renderHistory() {
  const space = state.space;
  const list = byId('history-list');
  byId('history-search').hidden = !space;
  list.setAttribute('aria-busy', String(Boolean(space?.loading)));
  list.replaceChildren(...(space?.chats || []).map(chat => {
    const row = element('div', 'history-row');
    row.dataset.chatId = chat.id;
    row.dataset.running = String(chatIsProcessing(chat));
    row.dataset.selected = String(state.view.record?.id === chat.id);
    const item = element('button', 'history-item');
    item.type = 'button';
    item.title = chat.title;
    if (state.view.record?.id === chat.id) item.setAttribute('aria-current', 'true');
    item.append(icon('chat'), element('span', '', chat.title));
    item.addEventListener('click', () => {
      sidebar(false);
      if (state.view.record?.id !== chat.id) showView(newView(chat));
    });
    const manage = element('button', 'icon-button history-manage');
    manage.type = 'button';
    manage.title = '管理会话';
    manage.setAttribute('aria-haspopup', 'dialog');
    manage.setAttribute('aria-label', `管理会话 ${chat.title}`);
    manage.append(icon('more'));
    manage.addEventListener('click', () => openConversation(chat));
    row.append(item, manage);
    return row;
  }));
  const empty = byId('history-empty');
  empty.hidden = Boolean(space?.chats.length);
  empty.textContent = state.user
    ? (space?.loading ? '正在读取会话…' : space?.error ? '会话暂时无法加载'
      : space?.search ? '没有找到匹配的标题' : '还没有会话')
    : '登录后可以查看你的会话';
  const more = byId('older-chats');
  more.hidden = !space?.cursor && !space?.error;
  more.disabled = Boolean(space?.loading);
  more.textContent = space?.error ? '重试加载' : '加载更多';
}

function stopHistory(space) {
  clearTimeout(space?.searchTimer);
  space?.request?.abort();
}

async function listChats(space, older = false) {
  if (state.space !== space || (older && (space.loading || !space.cursor))) return;
  stopHistory(space);
  const request = new AbortController();
  space.request = request;
  space.loading = true;
  space.error = false;
  renderHistory();
  try {
    const params = new URLSearchParams({ limit: '30' });
    if (space.search) params.set('q', space.search);
    if (older) params.set('cursor', space.cursor);
    const page = await api(`/v1/instances/${space.instance.id}/conversations?${params}`, { signal: request.signal });
    if (state.space !== space || space.request !== request) return;
    if (older) {
      const seen = new Set(space.chats.map(item => item.id));
      space.chats.push(...page.items.filter(item => !seen.has(item.id)));
    } else {
      space.chats = page.items;
    }
    space.cursor = page.next_cursor;
    for (const chat of page.items) {
      if (chat.active_run_id && !state.watches.has(chat.id)) watchRun(chat.id, chat.active_run_id);
    }
  } catch (err) {
    if (state.space !== space || space.request !== request) return;
    if (older && err.status === 404) {
      void listChats(space);
      return;
    }
    space.error = true;
    space.retryOlder = older;
  } finally {
    if (state.space === space && space.request === request) {
      space.request = null;
      space.loading = false;
      renderHistory();
    }
  }
}

function searchChats() {
  const space = state.space;
  const search = historyQuery.value.trim();
  if (!space || space.search === search) return;
  stopHistory(space);
  Object.assign(space, { search, request: null, chats: [], cursor: null, loading: true, error: false });
  renderHistory();
  space.searchTimer = setTimeout(() => void listChats(space), 230);
}

function chooseInstance(instance, preserveDraft = false) {
  stopHistory(state.space);
  historyQuery.value = '';
  const space = { instance, chats: [], cursor: null, search: '' };
  state.space = space;
  showView(newView(), preserveDraft);
  renderInstances();
  void listChats(space);
}

function preferredInstance() {
  return state.instances.find(instance => instance.is_default)
    || state.instances.find(instance => instance.status === 'active') || state.instances[0];
}

function refreshSelectedInstance() {
  const current = state.instances.find(instance => instance.id === state.space?.instance.id);
  if (current) {
    state.space.instance = current;
    byId('chat-instance').textContent = current.name;
  } else if (state.instances.length) {
    chooseInstance(preferredInstance(), !state.view.record && !state.space);
    return;
  } else if (state.space) {
    stopHistory(state.space);
    state.space = null;
    historyQuery.value = '';
    showView(newView());
  }
  renderInstances();
  renderRun(state.view);
}

async function listInstances() {
  if (state.user?.campus_status !== 'ready') return;
  const generation = state.generation;
  const request = ++state.instanceRequest;
  try {
    const data = await api('/v1/instances');
    if (generation !== state.generation || request !== state.instanceRequest) return;
    state.instancesLoaded = true;
    state.instances = data.items;
    refreshSelectedInstance();
  } catch (err) {
    if (generation === state.generation && request === state.instanceRequest) showNotice(err.message, { label: '重试', run: () => {
      showNotice();
      void listInstances();
    } });
  }
}

function bindUniversityWatermark() {
  const [top, bottom] = welcomeHero.querySelector('.hero-university').children;
  const context = document.createElement('canvas').getContext('2d');
  if (!context || !window.ResizeObserver) return;
  const observer = new ResizeObserver(() => {
    const { fontWeight, fontSize, fontFamily } = getComputedStyle(top);
    context.font = `${fontWeight} ${fontSize} ${fontFamily}`;
    context.textAlign = 'center';
    const topInk = context.measureText(top.textContent);
    const bottomInk = context.measureText(bottom.textContent);
    const topWidth = topInk.actualBoundingBoxLeft + topInk.actualBoundingBoxRight;
    const bottomWidth = bottomInk.actualBoundingBoxLeft + bottomInk.actualBoundingBoxRight;
    if (!(topWidth > 0 && bottomWidth > 0)) return;
    const scale = topWidth / bottomWidth;
    const shift = bottomInk.actualBoundingBoxLeft * scale - topInk.actualBoundingBoxLeft;
    bottom.style.setProperty('--watermark-scale-x', scale);
    bottom.style.setProperty('--watermark-shift-x', `${shift}px`);
  });
  observer.observe(top);
}

function updateHeroScroll() {
  const scrollTop = Math.max(0, window.scrollY);
  const fadeDistance = Math.max(1, landing.offsetHeight / 3);
  const workspaceReached = workspaceLayout.getBoundingClientRect().top <= 2;
  const progress = Math.min(1, scrollTop / fadeDistance);
  const background = heroThemeColor.map(channel => Math.round(channel + (255 - channel) * progress));
  const luminance = relativeLuminance(background);
  // Near this luminance, white gives more contrast than black on the same surface.
  welcomeHero.dataset.tone = luminance < .179 ? 'dark' : 'light';
  welcomeHero.style.setProperty('--hero-surface', `rgb(${background.join(' ')})`);
  welcomeHero.style.setProperty('--hero-scroll-progress', progress.toFixed(3));
  welcomeHero.style.setProperty('--hero-art-offset', `${Math.min(36, scrollTop * .23)}px`);
  workspaceLayout.classList.toggle('is-reached', workspaceReached);
}

function bindLandingScroll() {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const mark = welcomeHero.querySelector('.hero-mark');
  let wheelIdle;
  let touchStart;
  let entryTimer;

  const endHover = () => mark.classList.remove('is-hovered');
  mark.addEventListener('pointermove', event => {
    if (event.pointerType === 'mouse' && heroHoverMotion.matches && !welcomeHero.dataset.rings) {
      mark.classList.add('is-hovered');
    }
  }, { passive: true });
  mark.addEventListener('pointerleave', endHover);
  window.addEventListener('blur', endHover);
  heroHoverMotion.addEventListener('change', endHover);

  function turnRingsLeft() {
    if (welcomeHero.dataset.rings) return;
    const turning = mark.getAnimations({ subtree: true }).some(animation => animation.transitionProperty === 'transform');
    welcomeHero.dataset.rings = mark.classList.contains('is-hovered') || turning ? 'replay' : 'left';
    endHover();
  }

  function syncRings() {
    if (window.scrollY > 1) turnRingsLeft();
    else if (!entryTimer) delete welcomeHero.dataset.rings;
  }

  function cancelEntry() {
    clearTimeout(entryTimer);
    entryTimer = null;
    syncRings();
  }

  function enterWorkspace() {
    if (entryTimer) return;
    turnRingsLeft();
    const scrollToWorkspace = () => {
      entryTimer = null;
      workspaceLayout.scrollIntoView({ block: 'start', behavior: reducedMotion.matches ? 'auto' : 'smooth' });
    };
    // Start the turn while the rings are still fully visible.
    if (reducedMotion.matches || window.scrollY > 1) scrollToWorkspace();
    else entryTimer = setTimeout(scrollToWorkspace, 280);
  }

  window.addEventListener('scroll', syncRings, { passive: true });
  syncRings();

  workspaceLayout.addEventListener('focusin', event => {
    if (event.target.matches(':focus-visible') && !workspaceLayout.classList.contains('is-reached')) enterWorkspace();
  });

  landing.addEventListener('wheel', event => {
    if (event.ctrlKey || event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
    if (event.deltaY < 0) {
      clearTimeout(wheelIdle);
      wheelIdle = null;
      cancelEntry();
      return;
    }
    if (!event.deltaY) return;
    event.preventDefault();
    if (!wheelIdle) enterWorkspace();
    clearTimeout(wheelIdle);
    wheelIdle = setTimeout(() => { wheelIdle = null; }, 150);
  }, { passive: false });

  landing.addEventListener('touchstart', event => {
    touchStart = event.touches.length === 1 ? event.touches[0] : null;
  }, { passive: true });
  landing.addEventListener('touchend', event => {
    const start = touchStart;
    touchStart = null;
    const end = event.changedTouches[0];
    if (!start || event.touches.length || start.identifier !== end?.identifier) return;
    const distance = start.clientY - end.clientY;
    if (distance >= 28 && distance > Math.abs(start.clientX - end.clientX)) enterWorkspace();
  }, { passive: true });
  landing.addEventListener('touchcancel', () => { touchStart = null; }, { passive: true });

  window.addEventListener('keydown', event => {
    const onLanding = event.target === document.body || landing.contains(event.target);
    if (!onLanding || event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
    if (['PageUp', 'ArrowUp', 'Home'].includes(event.key)) cancelEntry();
    if (!['PageDown', 'ArrowDown', ' '].includes(event.key) || landing.getBoundingClientRect().bottom <= 1) return;
    event.preventDefault();
    if (!event.repeat) enterWorkspace();
  });
}

function bindHeroLens() {
  const lens = byId('hero-lens');
  const copy = welcomeHero.querySelector('.hero-copy');
  let pointer;
  let frame = 0;

  function hide() {
    pointer = null;
    cancelAnimationFrame(frame);
    frame = 0;
    lens.classList.remove('is-visible');
  }

  function paint() {
    frame = 0;
    const text = copy.getBoundingClientRect();
    if (pointer.x < text.left || pointer.y < text.top || pointer.x >= text.right || pointer.y >= text.bottom) {
      hide();
      return;
    }
    const bounds = welcomeHero.getBoundingClientRect();
    const x = pointer.x - bounds.left;
    const y = pointer.y - bounds.top;
    lens.style.setProperty('--lens-x', `${x}px`);
    lens.style.setProperty('--lens-y', `${y}px`);
    lens.classList.add('is-visible');
  }

  function schedulePaint() {
    if (pointer && !frame) frame = requestAnimationFrame(paint);
  }

  function trackPointer(event) {
    if (!heroHoverMotion.matches || event.pointerType !== 'mouse') return;
    pointer = { x: event.clientX, y: event.clientY };
    schedulePaint();
  }

  copy.addEventListener('pointerenter', trackPointer);
  copy.addEventListener('pointermove', trackPointer, { passive: true });
  copy.addEventListener('pointerleave', event => {
    if (event.pointerType === 'mouse') hide();
  });
  copy.addEventListener('pointercancel', hide);
  window.addEventListener('blur', hide);
  window.addEventListener('scroll', schedulePaint, { passive: true });
  window.addEventListener('resize', schedulePaint);
  heroHoverMotion.addEventListener('change', hide);
}

function syncCampusAvatar() {
  campusAvatar.hidden = !avatarPreference.checked || Boolean(state.view.record) || campusAvatar.dataset.state === 'unavailable';
}

function saveAvatarPreference() {
  try { localStorage.setItem(avatarStorageKey, avatarPreference.checked ? 'shown' : 'hidden'); } catch { /* Applies for this page. */ }
  syncCampusAvatar();
}

function showView(view, preserveDraft = false, read = true) {
  state.view = view;
  if (!preserveDraft) {
    query.value = '';
    target.value = '';
  }
  workspaceEmpty.hidden = Boolean(view.record);
  syncCampusAvatar();
  byId('chat').hidden = !view.record;
  byId('chat-title').textContent = view.record?.title || '';
  byId('chat-instance').textContent = state.space?.instance.name || '';
  viewport.scrollTop = 0;
  renderHistory();
  renderMessages(view);
  renderRun(view);
  if (view.record && read) void recoverRun(view);
}

function messageKey(message) {
  return message.run_id ? `${message.run_id}/${message.role}` : message.id;
}

function messageOrder(a, b) {
  return a.id - b.id;
}

let turnNavView;
let turnPreviewMark;
let turnScrollFrame;
// Keep the rail's manual position until the conversation itself moves.
let turnBrowseScrollTop = null;

function turnExcerpt(value, limit) {
  const plain = value.trim().replace(/\s+/g, ' ');
  if (plain.length <= limit) return plain;
  const characters = Array.from(plain.slice(0, limit * 2 + 2));
  return characters.length > limit ? characters.slice(0, limit).join('') + '…' : plain;
}

function updateOlderMessageButtons(view) {
  for (const button of [olderMessages, turnNavOlder]) {
    button.hidden = !view.cursor;
    button.disabled = Boolean(view.loadingMessages);
  }
}

function setTurnTabStop(button) {
  const previous = turnTicks.querySelector('[tabindex="0"]');
  if (previous && previous !== button) previous.tabIndex = -1;
  if (button) button.tabIndex = 0;
}

function renderTurnNav(view, turns) {
  const sameView = turnNavView === view;
  if (!sameView) turnBrowseScrollTop = null;
  const previous = sameView ? view.turns : [];
  const existing = new Map(previous.map(turn => [turn.key, turn.button]));
  for (const turn of turns) {
    const button = existing.get(turn.key) || element('button', 'turn-tick');
    button.type = 'button';
    if (!existing.has(turn.key)) button.tabIndex = -1;
    button.setAttribute('aria-controls', turn.anchor.id);
    button.setAttribute('aria-label', `跳转到提问「${turn.question}」`);
    turn.button = button;
  }
  turnNavView = view;
  view.turns = turns;
  turnNav.hidden = !view.record || (!turns.length && !view.cursor);
  updateOlderMessageButtons(view);
  if (!sameView || previous.length !== turns.length || turns.some((turn, index) => turn.button !== previous[index]?.button)) {
    const focused = sameView && turnTicks.contains(document.activeElement) ? document.activeElement : null;
    const offset = sameView ? turnTicks.scrollTop : 0;
    turnTicks.replaceChildren(...turns.map(turn => turn.button));
    turnTicks.scrollTop = offset;
    if (focused?.isConnected) focused.focus({ preventScroll: true });
  }
  if (!turnTicks.querySelector('[tabindex="0"]')) setTurnTabStop(turns.at(-1)?.button);
  if (turnPreviewMark) showTurnPreview(turnPreviewMark);
  scheduleTurnIndicator();
}

function showTurnPreview(button) {
  const turn = state.view.turns.find(item => item.button === button);
  if (!turn || turnNav.hidden) {
    turnPreviewMark = null;
    turnPreview.hidden = true;
    return;
  }
  const mark = button.getBoundingClientRect();
  const list = turnTicks.getBoundingClientRect();
  if (mark.bottom <= list.top || mark.top >= list.bottom) {
    showTurnPreview(null);
    return;
  }
  turnPreviewMark = button;
  byId('turn-preview-time').textContent = turn.time ? `你的提问 · ${turn.time}` : '你的提问';
  byId('turn-preview-question').textContent = turn.question;
  const answer = byId('turn-preview-answer');
  answer.textContent = turn.answer;
  answer.hidden = !turn.answer;
  turnPreview.hidden = false;
  const top = mark.top - turnNav.getBoundingClientRect().top + (mark.height - turnPreview.offsetHeight) / 2;
  turnPreview.style.insetBlockStart = `${Math.max(0, Math.min(top, turnNav.clientHeight - turnPreview.offsetHeight))}px`;
}

function highlightTurn(button) {
  const previous = turnTicks.querySelector('[aria-current="location"]');
  if (previous !== button) {
    previous?.removeAttribute('aria-current');
    button.setAttribute('aria-current', 'location');
  }
  if (!turnTicks.contains(document.activeElement)) setTurnTabStop(button);
}

function revealTurnTick(button) {
  const list = turnTicks.getBoundingClientRect();
  const mark = button.getBoundingClientRect();
  if (mark.top < list.top) turnTicks.scrollTop -= list.top - mark.top;
  else if (mark.bottom > list.bottom) turnTicks.scrollTop += mark.bottom - list.bottom;
}

function browseTurnTicks() {
  turnBrowseScrollTop = viewport.scrollTop;
}

function scheduleTurnIndicator() {
  if (turnScrollFrame) return;
  turnScrollFrame = requestAnimationFrame(() => {
    turnScrollFrame = null;
    const turns = state.view.turns;
    if (turnNav.hidden || !turns.length) return;
    const scrollTop = viewport.scrollTop;
    const maxScroll = viewport.scrollHeight - viewport.clientHeight;
    let active = turns.at(-1).button;
    if (maxScroll <= 2 || scrollTop < maxScroll - 2) {
      const edge = viewport.getBoundingClientRect().top + Math.min(90, viewport.clientHeight * .2);
      let start = 0;
      let end = turns.length;
      while (start < end) {
        const middle = Math.floor((start + end) / 2);
        if (turns[middle].anchor.getBoundingClientRect().top <= edge) start = middle + 1;
        else end = middle;
      }
      active = turns[Math.max(0, start - 1)].button;
    }
    highlightTurn(active);
    if (turnBrowseScrollTop === scrollTop) return;
    turnBrowseScrollTop = null;
    revealTurnTick(active);
  });
}

function bindTurnNavigation() {
  viewport.addEventListener('scroll', scheduleTurnIndicator, { passive: true });
  const resize = new ResizeObserver(() => {
    scheduleTurnIndicator();
    if (turnPreviewMark) showTurnPreview(turnPreviewMark);
  });
  resize.observe(viewport);
  resize.observe(byId('messages'));
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && turnPreviewMark) showTurnPreview(null);
  });
  turnTicks.addEventListener('scroll', () => {
    if (turnPreviewMark) showTurnPreview(turnPreviewMark);
  }, { passive: true });
  turnTicks.addEventListener('wheel', browseTurnTicks, { passive: true });
  turnTicks.addEventListener('pointerdown', event => {
    if (event.pointerType === 'touch') browseTurnTicks();
  }, { passive: true });
  turnTicks.addEventListener('pointerover', event => {
    if (event.pointerType !== 'touch') showTurnPreview(event.target.closest('.turn-tick'));
  });
  turnTicks.addEventListener('pointerout', event => {
    if (!event.relatedTarget?.closest?.('.turn-tick')) showTurnPreview(null);
  });
  turnTicks.addEventListener('focusin', event => {
    const button = event.target.closest('.turn-tick');
    if (button) {
      setTurnTabStop(button);
      if (button.matches(':focus-visible')) {
        browseTurnTicks();
        showTurnPreview(button);
      }
    }
  });
  turnTicks.addEventListener('focusout', event => {
    if (!turnTicks.contains(event.relatedTarget)) showTurnPreview(null);
  });
  turnTicks.addEventListener('keydown', event => {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    const turns = state.view.turns;
    const index = turns.findIndex(turn => turn.button === event.target);
    if (index < 0) return;
    event.preventDefault();
    browseTurnTicks();
    const next = { ArrowUp: index - 1, ArrowDown: index + 1, Home: 0, End: turns.length - 1 }[event.key];
    const button = turns[Math.max(0, Math.min(next, turns.length - 1))].button;
    revealTurnTick(button);
    button.focus({ preventScroll: true });
  });
  turnTicks.addEventListener('click', event => {
    const turn = state.view.turns.find(item => item.button === event.target.closest('.turn-tick'));
    if (!turn) return;
    turnBrowseScrollTop = null;
    showTurnPreview(null);
    highlightTurn(turn.button);
    const top = viewport.scrollTop + turn.anchor.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 14;
    viewport.scrollTo({ top, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    if (event.detail === 0) turn.anchor.focus({ preventScroll: true });
    scheduleTurnIndicator();
  });
}

function turnActions(view, message) {
  const group = element('div', 'message-actions');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', '本轮操作');
  if (terminal.has(message.run_status) && !message.withdrawal_started_at) {
    const retry = element('button', 'turn-button');
    retry.type = 'button';
    retry.append(icon('retry'), element('span', '', '重试'));
    retry.disabled = Boolean(view.runId || state.pending.has(view.record.id) || state.space?.instance.status === 'paused');
    retry.addEventListener('click', () => retryTurn(view, message));
    group.append(retry);
  } else if (message.role === 'assistant' && message.run_status && !terminal.has(message.run_status)) {
    group.append(element('span', 'turn-waiting', '正在重新生成…'));
  }
  const withdraw = element('button', 'turn-button');
  withdraw.type = 'button';
  withdraw.append(icon('withdraw'), element('span', '', '撤回'));
  withdraw.setAttribute('aria-haspopup', 'dialog');
  withdraw.addEventListener('click', () => confirmWithdrawal(view, message.run_id));
  group.append(withdraw);
  return group;
}

function renderMessages(view) {
  const list = byId('messages');
  const last = view.messages.at(-1);
  const lastKey = last && messageKey(last);
  const pending = state.pending.get(view.record?.id);
  const turns = [];
  const byRun = new Map();
  list.replaceChildren(...view.messages.map(message => {
    const isUser = message.role === 'user';
    const item = element('article', isUser ? 'message message-user' : 'message');
    if (message === last && view.displayedLast !== lastKey) item.classList.add('message-new');
    const avatar = element('span', isUser ? 'avatar' : 'avatar cuc-emblem');
    avatar.setAttribute('aria-hidden', 'true');
    if (isUser) avatar.append(icon('user'));
    const body = element('div', 'message-body');
    const meta = element('div', 'message-meta');
    meta.append(element('strong', '', isUser ? '你' : '校园助手'));
    const time = new Date(message.created_at);
    if (!Number.isNaN(time.valueOf())) meta.append(element('time', '', timeFormat.format(time)));
    const useMarkdown = message.role === 'assistant' && Boolean(renderMarkdown);
    const copy = element(useMarkdown ? 'div' : 'p', 'message-copy');
    if (useMarkdown) {
      copy.classList.add('message-markdown');
      copy.innerHTML = renderMarkdown(message.content);
    } else {
      copy.textContent = message.content;
    }
    if (isUser) {
      item.id = `chat-turn-${message.id}`;
      item.tabIndex = -1;
      const turn = { key: messageKey(message), runId: message.run_id, anchor: item,
        question: turnExcerpt(message.content, 100) || '这轮提问', answer: '',
        time: Number.isNaN(time.valueOf()) ? '' : timeFormat.format(time) };
      turns.push(turn);
      if (message.run_id) byRun.set(message.run_id, turn);
    } else {
      const turn = message.run_id ? byRun.get(message.run_id) : turns.at(-1);
      if (turn && turn.runId === message.run_id) turn.answer = turnExcerpt(copy.textContent, 190);
    }
    body.append(meta, copy);
    if (message === last && message.run_id && (!pending || pending.runId === message.run_id)) {
      body.append(turnActions(view, message));
    }
    item.append(avatar, body);
    return item;
  }));
  view.displayedLast = lastKey;
  if (!view.messages.length && view.record) {
    list.append(element('p', 'message-empty', view.loaded ? '发送一个问题，开始这个会话。' : '正在读取会话…'));
  }
  renderTurnNav(view, turns);
}

async function readMessages(view, older = false) {
  if (older && view.loadingMessages) return;
  while (state.view === view && view.loadingMessages) await view.loadingMessages.catch(() => {});
  if (state.view !== view) return;
  const cursor = older ? view.cursor : null;
  const task = api(`/v1/conversations/${view.record.id}/messages?limit=40${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`);
  view.loadingMessages = task;
  updateOlderMessageButtons(view);
  try {
    const page = await task;
    if (state.view !== view) return;
    const offset = viewport.scrollHeight - viewport.scrollTop;
    const keepBottom = offset - viewport.clientHeight < 80 || !view.loaded;
    const items = page.items.filter(item => !state.withdrawnRuns.has(item.run_id));
    if (older) {
      const known = new Set(view.messages.map(messageKey));
      view.messages.unshift(...items.filter(item => !known.has(messageKey(item))));
      view.cursor = page.next_cursor;
    } else {
      const earliest = items[0];
      const earlier = earliest && page.next_cursor ? view.messages.filter(item => messageOrder(item, earliest) < 0) : [];
      view.messages = earlier.concat(items);
      if (!earlier.length) view.cursor = page.next_cursor;
      view.loaded = true;
    }
    renderMessages(view);
    if (older) viewport.scrollTop = viewport.scrollHeight - offset;
    else if (keepBottom) viewport.scrollTop = viewport.scrollHeight;
  } finally {
    view.loadingMessages = null;
    if (state.view === view) {
      updateOlderMessageButtons(view);
      renderRun(view);
    }
  }
}

async function loadOlderMessages(fromTurnNav = false) {
  const view = state.view;
  if (!view.cursor || view.loadingMessages) return;
  const focused = fromTurnNav && document.activeElement === turnNavOlder;
  try {
    await readMessages(view, true);
    if (fromTurnNav && state.view === view) {
      turnTicks.scrollTop = 0;
      browseTurnTicks();
      if (focused && turnNavOlder.hidden) view.turns[0]?.button.focus({ preventScroll: true });
    }
  } catch (err) {
    status(view, err.message, { error: true, label: '重试读取', action: () => loadOlderMessages(fromTurnNav) });
  }
}

function renderRun(view) {
  if (state.view !== view) return;
  const attempt = state.pending.get(view.record?.id);
  const status = attempt ? (attempt.sending
    ? { text: attempt.runId ? '正在提交重试请求…' : '正在提交问题…' }
    : { text: '提交状态未确认。重试会沿用同一个请求。', error: true,
      label: '重试提交', action: () => submitTurn(view.record.id, attempt) }) : view.status;
  const node = byId('run-status');
  node.hidden = !status;
  node.dataset.tone = status?.error ? 'error' : 'info';
  const working = Boolean(!status?.error && (attempt?.sending || view.runId || view.creating));
  node.dataset.pending = String(working);
  const latest = view.messages.at(-1);
  updateChatCompanion(view.record?.id, working, !status?.error && latest?.role === 'assistant' && latest.run_status === 'succeeded');
  const statusText = byId('run-status-text');
  const text = status?.text || '';
  if (statusText.textContent !== text) statusText.replaceChildren(element('span', '', text));
  const action = byId('run-action');
  action.hidden = !status?.action;
  action.textContent = status?.label || '';
  action.onclick = status?.action || null;
  const paused = state.space?.instance.status === 'paused';
  const loadingInstances = Boolean(state.user && !state.space && !state.instancesLoaded);
  byId('new-chat').disabled = Boolean(paused || loadingInstances);
  byId('send').disabled = Boolean(paused || loadingInstances || !view.loaded || view.creating || attempt || view.runId || !query.value.trim());
  byId('composer-hint').textContent = paused ? '该实例已暂停，可以查看已有会话'
    : !state.user ? '登录后即可发送问题并保存会话'
      : loadingInstances ? '正在读取你的实例…'
        : !state.space ? '创建一个实例后即可发送问题'
          : 'Enter 发送 · Shift + Enter 换行';
}

function status(view, text, options = {}) {
  view.status = text ? { text, ...options } : null;
  renderRun(view);
}

async function recoverRun(view) {
  try {
    await readMessages(view);
    if (state.view !== view || state.pending.has(view.record.id)) return;
    const latest = view.messages.at(-1);
    const needsStatus = latest?.run_id && (!latest.run_status || activeRuns.has(latest.run_status));
    const runId = needsStatus ? latest.run_id : view.record.active_run_id || view.runId;
    if (runId) watchRun(view.record.id, runId, { attempt: latest?.run_id === runId ? latest.run_attempt : undefined });
    else if (view.status?.label === '重试读取') status(view);
  } catch (err) {
    status(view, err.message, { error: true, label: '重试读取', action: () => recoverRun(view) });
  }
}

async function cancelRun(view, runId) {
  if (view.cancelFor === runId) return;
  view.cancelFor = runId;
  status(view, '正在请求停止…');
  try {
    await api(`/v1/runs/${runId}/cancel`, { method: 'POST' });
  } catch (err) {
    view.cancelFor = null;
    if (view.runId === runId) status(view, err.message, { error: true, label: '重试停止', action: () => cancelRun(view, runId) });
  }
}

function updateRunMessages(view, run) {
  const attempt = run.attempt ?? 1;
  let changed = false;
  for (const message of view.messages) {
    if (message.run_id !== run.id || (message.run_attempt ?? 1) > attempt) continue;
    if ((message.run_attempt ?? 1) === attempt && terminal.has(message.run_status) && activeRuns.has(run.status)) continue;
    const withdrawal = run.withdrawal_started_at ?? message.withdrawal_started_at;
    if (message.run_status === run.status && message.run_attempt === attempt && message.withdrawal_started_at === withdrawal) continue;
    message.run_status = run.status;
    message.run_attempt = attempt;
    message.withdrawal_started_at = withdrawal;
    changed = true;
  }
  return changed;
}

function showProgress(view, run) {
  if (updateRunMessages(view, run)) renderMessages(view);
  if (run.cancel_requested_at || view.cancelFor === run.id) return status(view, '正在停止本次运行…');
  const agents = run.status === 'running' && Array.isArray(run.active_agents)
    ? run.active_agents.map(id => subagentNames.get(id)).filter(Boolean) : [];
  const text = run.status === 'queued' ? '正在排队…'
    : agents.length ? `正在调用「${agents.join('、')}」…`
      : run.attempt > 1 ? '校园助手正在重新生成…' : '校园助手正在整理回复…';
  status(view, text, { label: '停止', action: () => cancelRun(view, run.id) });
}

function watchRun(conversationId, runId, { attempt = 0 } = {}) {
  if (state.withdrawnRuns.has(runId)) return;
  const previous = state.watches.get(conversationId);
  const view = currentChatView(conversationId);
  if (view) view.runId = runId;
  if (previous?.runId === runId && previous.attempt >= attempt) {
    if (view && activeRuns.has(previous.progress?.status)) showProgress(view, previous.progress);
    return;
  }
  previous?.controller.abort();
  const watch = { runId, attempt, controller: new AbortController(), progress: null };
  state.watches.set(conversationId, watch);
  setChatActivity(conversationId, runId);
  if (view) status(view, '正在读取处理进度…');
  void pollRun(conversationId, watch);
}

async function pollRun(conversationId, watch) {
  const { runId } = watch;
  const current = () => state.watches.get(conversationId) === watch;
  let failures = 0;
  try {
    while (current()) {
      let run;
      try {
        run = await api(`/v1/runs/${runId}`, { signal: watch.controller.signal });
      } catch (err) {
        const transient = err.status === 0 || err.status === 429 || err.status >= 500;
        if (!current() || !transient || failures === 3) throw err;
        failures += 1;
        await new Promise(resolve => setTimeout(resolve, 1500 * failures));
        continue;
      }
      failures = 0;
      if (!current()) return;
      if (run.withdrawn_at) {
        hideWithdrawnTurn(conversationId, runId);
        return;
      }
      const view = currentChatView(conversationId);
      if (terminal.has(run.status)) {
        watch.progress = run;
        let readError;
        if (view) {
          status(view, '正在读取本轮结果…');
          try {
            await readMessages(view);
          } catch (err) {
            readError = err;
          }
        }
        if (!current()) return;
        state.watches.delete(conversationId);
        setChatActivity(conversationId, null, runId);
        if (run.steps?.some(step => step.step_id === 'minta-action')) campusPanels.refreshActivities();
        const visible = currentChatView(conversationId);
        if (visible && visible === view) showCompletedTurn(view, run, readError);
        else if (visible) {
          if (visible.runId === runId) visible.runId = visible.cancelFor = null;
          void recoverRun(visible);
        }
        return;
      }
      watch.attempt = run.attempt ?? watch.attempt;
      watch.progress = run;
      if (view) {
        view.runId = runId;
        showProgress(view, run);
      }
      await new Promise(resolve => setTimeout(resolve, 1300));
    }
  } catch (err) {
    if (!current()) return;
    state.watches.delete(conversationId);
    const view = currentChatView(conversationId);
    if (err.status === 404) {
      hideWithdrawnTurn(conversationId, runId);
    } else if (view) status(view, err.message, { error: true, label: '重新读取进度',
      action: () => watchRun(conversationId, runId, { attempt: watch.attempt }) });
  }
}

function showCompletedTurn(view, run, readError) {
  if (view.runId === run.id) view.runId = view.cancelFor = null;
  const keepBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
  if (readError && !state.withdrawnRuns.has(run.id) && Array.isArray(run.messages) && run.messages.length) {
    view.messages = view.messages.filter(item => item.run_id !== run.id).concat(run.messages).sort(messageOrder);
  }
  updateRunMessages(view, run);
  renderMessages(view);
  if (keepBottom) viewport.scrollTop = viewport.scrollHeight;
  if (view.runId) return;
  if (readError) {
    status(view, readError.message, { error: true, label: '重试读取', action: () => recoverRun(view) });
    return;
  }
  const latest = view.messages.at(-1);
  if (latest?.run_id !== run.id || (latest.run_attempt ?? 1) > (run.attempt ?? 1)) {
    if (latest?.run_id && activeRuns.has(latest.run_status)) watchRun(view.record.id, latest.run_id, { attempt: latest.run_attempt });
    else status(view);
    return;
  }
  const missingReply = run.status === 'succeeded' && latest.role === 'user';
  const summary = run.withdrawal_started_at ? '这轮的校务撤回待完成'
    : run.status === 'cancelled' ? '本次运行已停止'
      : resultCopy[run.result_status] || (run.status === 'failed' ? '本次对话运行失败' : missingReply ? '这轮没有可显示的回复，可以重试。' : '');
  status(view, summary, { error: run.status === 'failed' || missingReply });
}

function hideWithdrawnTurn(conversationId, runId) {
  state.withdrawnRuns.add(runId);
  const watch = state.watches.get(conversationId);
  if (watch?.runId === runId) {
    watch.controller.abort();
    state.watches.delete(conversationId);
  }
  if (state.pending.get(conversationId)?.runId === runId) state.pending.delete(conversationId);
  setChatActivity(conversationId, null, runId);
  const view = currentChatView(conversationId);
  if (!view) return;
  view.messages = view.messages.filter(item => item.run_id !== runId);
  if (view.runId === runId) view.runId = view.cancelFor = null;
  renderMessages(view);
  if (!view.runId) status(view);
  if (view.loaded && !view.messages.length && view.cursor) void readMessages(view).catch(() => {});
}

function retryTurn(view, message) {
  const id = view.record.id;
  if (state.view !== view || view.messages.at(-1) !== message || view.runId ||
      state.pending.has(id) || state.space?.instance.status === 'paused') return;
  const attempt = { runId: message.run_id, data: { attempt: message.run_attempt || 1 },
    key: crypto.randomUUID(), instanceId: view.record.instance_id };
  state.pending.set(id, attempt);
  void submitTurn(id, attempt);
}

async function submitTurn(id, attempt) {
  if (attempt.sending) return;
  const generation = state.generation;
  attempt.sending = true;
  setChatActivity(id);
  const openView = currentChatView(id);
  if (openView) renderMessages(openView);
  renderRun(state.view);
  try {
    const path = attempt.runId ? `/v1/runs/${attempt.runId}/retry` : `/v1/conversations/${id}/runs`;
    const run = await api(path, { method: 'POST', data: attempt.data, key: attempt.key });
    if (generation !== state.generation || state.pending.get(id) !== attempt) return;
    state.pending.delete(id);
    const view = currentChatView(id);
    if (run.withdrawn_at || state.withdrawnRuns.has(run.id)) {
      hideWithdrawnTurn(id, run.id);
      return;
    }
    if (view && !attempt.runId && query.value.trim() === attempt.data.query) query.value = '';
    watchRun(id, run.id, { attempt: run.attempt });
    if (view && attempt.runId && !terminal.has(run.status)) showProgress(view, run);
    if (view && !attempt.runId) {
      try {
        await readMessages(view);
        if (state.view === view) viewport.scrollTop = viewport.scrollHeight;
      } catch {
        // The run watcher reloads the saved turn when it completes.
      }
    }
  } catch (err) {
    if (generation !== state.generation || state.pending.get(id) !== attempt) return;
    attempt.sending = false;
    setChatActivity(id);
    const view = state.view;
    if (err.status === 0 || err.status >= 500) {
      renderRun(view);
      return;
    }
    state.pending.delete(id);
    if (view.record?.id === id) {
      renderMessages(view);
      status(view, err.message, { error: true });
      if (err.code === 'instance_paused') void listInstances();
      if (err.status === 409) void recoverRun(view);
    }
  }
}

async function send() {
  const text = query.value.trim();
  const original = state.view;
  if (!text || byId('send').disabled) return;
  if (!state.user) return openAuth('login');
  if (!state.space) return openInstance();
  const space = state.space;
  const selectedTarget = target.value;
  let view = original;
  if (!view.record) {
    original.creating = true;
    status(original, '正在创建会话…');
    try {
      const title = Array.from(text.replace(/\s+/g, ' ')).slice(0, 32).join('');
      const chat = await api(`/v1/instances/${space.instance.id}/conversations`, { method: 'POST', data: { title } });
      if (state.space !== space) return;
      void listChats(space);
      if (state.view !== original) return;
      view = newView(chat);
      view.loaded = true;
      showView(view, true, false);
    } catch (err) {
      original.creating = false;
      status(original, err.message, { error: true });
      return;
    }
  }
  const attempt = {
    data: { query: text, targets: selectedTarget ? [selectedTarget] : [] },
    key: crypto.randomUUID(), instanceId: space.instance.id,
  };
  state.pending.set(view.record.id, attempt);
  await submitTurn(view.record.id, attempt);
}

function mailKey(purpose, subject) {
  return subject ? `${purpose}:${subject.trim().toLowerCase().normalize()}` : '';
}

function remainingMailTime(key) {
  return Math.max(0, Math.ceil(((mailDeadlines.get(key) || 0) - Date.now()) / 1000));
}

function startMailCooldown(key, seconds = resendSeconds) {
  if (key && Number.isSafeInteger(seconds) && seconds > 0) {
    mailDeadlines.set(key, Math.max(mailDeadlines.get(key) || 0, Date.now() + seconds * 1000));
  }
  renderMailButtons();
}

function renderMailButtons() {
  clearTimeout(mailTimer);
  for (const [key, deadline] of mailDeadlines) {
    if (deadline <= Date.now()) mailDeadlines.delete(key);
  }
  const controls = [
    [byId('verification-resend'), mailKey('verify', byId('verification-email').value), state.authBusy, '发送验证邮件', authDialog],
    [byId('reset-send'), mailKey('reset', byId('reset-email').value), state.authBusy, '发送重置邮件', authDialog],
    [byId('password-change-send'), mailKey('change', state.user?.id), Boolean(passwordOperation),
      passwordOperation?.sending ? '正在发送…' : '发送邮箱验证令牌', passwordDialog],
  ];
  let ticking = false;
  for (const [button, key, busy, label, dialog] of controls) {
    const remaining = remainingMailTime(key);
    button.disabled = busy || remaining > 0;
    button.textContent = remaining ? `${remaining} 秒后可重发` : label;
    ticking ||= remaining > 0 && dialog.open && !button.closest('form').hidden;
  }
  byId('password-change-submit').disabled = Boolean(passwordOperation);
  if (ticking) mailTimer = setTimeout(renderMailButtons, 250);
}

function openAuth(mode, message = '', error = false) {
  state.authMode = mode;
  const [title, intro] = authCopy[mode];
  byId('auth-title').textContent = title;
  byId('auth-intro').textContent = intro;
  for (const form of authDialog.querySelectorAll('[data-auth-form]')) form.hidden = form.dataset.authForm !== mode;
  if (mode === 'verification-request') byId('verification-email').value = verificationEmail;
  if (mode === 'password-request' && resetEmail) byId('reset-email').value = resetEmail;
  feedback('auth-feedback', message, error);
  if (!authDialog.open) authDialog.showModal();
  renderMailButtons();
  authDialog.querySelector('form:not([hidden]) input')?.focus();
}

function formData(form) {
  return Object.fromEntries([...new FormData(form)].filter(([name, value]) => name !== 'display_name' || value.trim()));
}

async function handleAuth(form) {
  if (state.authBusy) return;
  const mode = form.dataset.authForm;
  const button = form.querySelector('[type="submit"]');
  const data = formData(form);
  if (mode === 'register') {
    if (data.password !== data.password_confirmation) {
      feedback('auth-feedback', '两次输入的密码不一致。', true);
      form.elements.password_confirmation.focus();
      return;
    }
    delete data.password_confirmation;
  }
  const purpose = mode === 'verification-request' ? 'verify' : mode === 'password-request' ? 'reset' : null;
  const key = purpose && mailKey(purpose, data.email);
  if (key && remainingMailTime(key)) return;
  if (mode === 'login') data.remember_me = form.elements.remember_me.checked;
  state.authBusy = button.disabled = true;
  form.setAttribute('aria-busy', 'true');
  renderMailButtons();
  feedback('auth-feedback', mode === 'login' ? '正在登录…' : '正在处理，请稍候…');
  try {
    await api(form.dataset.path, { method: 'POST', data, guest: true });
    if (mode === 'register' || mode === 'verification-request') {
      verificationEmail = data.email;
      startMailCooldown(mailKey('verify', verificationEmail));
    } else if (mode === 'password-request') {
      resetEmail = data.email;
      startMailCooldown(key);
    }
    form.reset();
    if (mode === 'login') {
      const user = await api('/v1/me', { guest: true });
      const shortcut = loginShortcut;
      loginShortcut = null;
      replaceUser(user, true);
      showNotice();
      if (user.campus_status === 'ready') {
        void listInstances();
        if (shortcut) openCampusApp(shortcut);
        else query.focus();
      } else {
        void openProfile();
      }
    } else if (mode === 'verify' || mode === 'reset') {
      const email = mode === 'verify' ? verificationEmail : resetEmail;
      if (email) authDialog.querySelector('[data-auth-form=login]').elements.email.value = email;
      openAuth('login', mode === 'verify' ? '邮箱已验证，现在可以登录。' : '密码已重置，请使用新密码登录。');
    } else {
      const verification = mode !== 'password-request';
      openAuth(verification ? 'verify' : 'reset', '请求已受理，请查看邮箱中的一次性令牌。');
    }
  } catch (err) {
    if (mode === 'login' && err.code === 'email_not_verified') {
      verificationEmail = data.email;
      form.elements.password.value = '';
      openAuth('verify', '请先验证邮箱后再登录。', true);
    } else {
      if (key && err.code === 'email_cooldown') startMailCooldown(key, err.retryAfter || resendSeconds);
      feedback('auth-feedback', err.message, true);
    }
  } finally {
    state.authBusy = button.disabled = false;
    form.removeAttribute('aria-busy');
    renderMailButtons();
  }
}

function renderInstanceDirectory() {
  const list = byId('instance-directory-list');
  list.replaceChildren(...state.instances.map(instance => {
    const item = element('button', 'instance-option');
    item.type = 'button';
    item.title = instance.name;
    item.setAttribute('aria-pressed', String(instanceEditor?.instance?.id === instance.id));
    item.disabled = Boolean(instanceEditor?.busy);
    const status = instance.status === 'paused' ? '已暂停' : '已启用';
    item.append(icon('stack'), element('strong', '', instance.name),
      element('small', '', instance.is_default ? `默认 · ${status}` : status));
    item.addEventListener('click', () => {
      editInstance(instance);
      byId('instance-form').elements.name.focus();
    });
    return item;
  }));
}

function editInstance(instance) {
  instanceEditor = { instance, busy: false };
  const form = byId('instance-form');
  form.elements.name.value = instance?.name || '';
  form.elements.status.value = instance?.status || 'active';
  form.elements.status.disabled = !instance;
  form.elements.is_default.checked = Boolean(instance?.is_default);
  form.elements.is_default.disabled = !instance || instance.is_default;
  byId('instance-default-copy').textContent = instance?.is_default ? '当前默认实例' : '设为默认实例';
  byId('instance-status-label').hidden = !instance;
  byId('instance-default-label').hidden = !instance;
  byId('instance-delete').hidden = !instance;
  byId('instance-delete').disabled = false;
  byId('instance-submit').disabled = false;
  byId('instance-submit').textContent = instance ? '保存设置' : '创建实例';
  feedback('instance-feedback');
  if (instance) renderInstanceDirectory();
}

function openInstance(instance = null) {
  if (!state.user) return openAuth('login');
  if (instanceDialog.open) return;
  instanceDialog.dataset.mode = instance ? 'manage' : 'create';
  byId('instance-directory').hidden = !instance;
  byId('instance-title').textContent = instance ? '实例管理' : '创建实例';
  byId('instance-intro').textContent = instance
    ? '在这里改名、设置默认实例或暂停接收新任务。暂停时仍可查看历史会话。'
    : '每个实例拥有独立的会话列表。';
  editInstance(instance);
  instanceDialog.showModal();
  byId('instance-form').elements.name.focus();
}

async function handleInstance(form) {
  const button = form.querySelector('[type="submit"]');
  const generation = state.generation;
  const editor = instanceEditor;
  if (!editor || editor.busy) return;
  const existing = editor.instance;
  const name = form.elements.name.value.trim();
  const status = form.elements.status.value;
  const data = {};
  if (!existing || name !== existing.name) data.name = name;
  if (existing && status !== existing.status) data.status = status;
  if (existing && !existing.is_default && form.elements.is_default.checked) data.is_default = true;
  if (!name) return feedback('instance-feedback', '实例名称不能为空。', true);
  if (existing && !Object.keys(data).length) return instanceDialog.close();
  editor.busy = button.disabled = byId('instance-delete').disabled = true;
  if (existing) renderInstanceDirectory();
  feedback('instance-feedback');
  try {
    const instance = existing
      ? await api(`/v1/instances/${existing.id}`, { method: 'PATCH', data })
      : await api('/v1/instances', { method: 'POST', data });
    if (generation !== state.generation) return;
    if (existing) {
      state.instances = state.instances.map(item => {
        if (item.id === instance.id) return instance;
        return data.is_default && item.is_default ? { ...item, is_default: false } : item;
      });
      refreshSelectedInstance();
    } else {
      state.instances.unshift(instance);
      chooseInstance(instance, !state.view.record);
    }
    if (editor === instanceEditor && instanceDialog.open) instanceDialog.close();
    toast(existing ? '实例设置已保存' : '实例已创建');
    void listInstances();
  } catch (err) {
    if (editor === instanceEditor) feedback('instance-feedback', err.message, true);
  } finally {
    if (editor === instanceEditor) {
      editor.busy = button.disabled = byId('instance-delete').disabled = false;
      if (existing && instanceDialog.open) renderInstanceDirectory();
    }
  }
}

function openConversation(chat) {
  if (!chat || !state.space || !state.user || conversationDialog.open) return;
  conversationEditor = { chat, busy: false };
  const form = byId('conversation-form');
  form.elements.title.value = chat.title;
  byId('conversation-intro').textContent = `所属实例 · ${state.space.instance.name}`;
  byId('conversation-submit').disabled = byId('conversation-delete').disabled = false;
  feedback('conversation-feedback');
  conversationDialog.showModal();
  form.elements.title.focus();
}

async function handleConversation(form) {
  const editor = conversationEditor;
  if (!editor || editor.busy) return;
  const title = form.elements.title.value.trim();
  if (!title) return feedback('conversation-feedback', '会话标题不能为空。', true);
  if (title === editor.chat.title) return conversationDialog.close();
  const generation = state.generation;
  const submit = byId('conversation-submit');
  const remove = byId('conversation-delete');
  editor.busy = submit.disabled = remove.disabled = true;
  feedback('conversation-feedback');
  try {
    const chat = await api(`/v1/conversations/${editor.chat.id}`, { method: 'PATCH', data: { title } });
    if (generation !== state.generation) return;
    if (state.view.record?.id === chat.id) {
      state.view.record = chat;
      byId('chat-title').textContent = chat.title;
    }
    if (state.space?.instance.id === chat.instance_id) {
      state.space.chats = state.space.chats.map(item => item.id === chat.id ? chat : item);
      void listChats(state.space);
    }
    if (conversationEditor === editor && conversationDialog.open) conversationDialog.close();
    toast('会话标题已更新');
  } catch (err) {
    if (conversationEditor === editor) feedback('conversation-feedback', err.message, true);
  } finally {
    if (conversationEditor === editor) editor.busy = submit.disabled = remove.disabled = false;
  }
}

function confirmDeletion(kind, record) {
  if (!record || deleteDialog.open) return;
  const isInstance = kind === 'instance';
  const label = isInstance ? '实例' : '会话';
  deletion = { kind, record, generation: state.generation, busy: false };
  byId('delete-title').textContent = `删除${label}？`;
  const affected = isInstance ? '实例下的会话、消息和运行记录' : '会话下的消息和运行记录';
  const replacement = isInstance && state.instances.length === 1 ? '系统会创建一个空的默认实例。' : '';
  byId('delete-intro').textContent = `将永久删除「${isInstance ? record.name : record.title}」及${affected}。${replacement}`;
  byId('delete-cancel').disabled = byId('delete-confirm').disabled = false;
  byId('delete-cancel').textContent = '取消';
  byId('delete-confirm').textContent = '永久删除';
  feedback('delete-feedback');
  deleteDialog.showModal();
}

function confirmWithdrawal(view, runId) {
  if (deleteDialog.open || state.view !== view || view.messages.at(-1)?.run_id !== runId) return;
  const removal = { kind: 'turn', runId, conversationId: view.record.id, plan: null, step: 0 };
  deletion = removal;
  byId('delete-title').textContent = '撤回这轮消息？';
  byId('delete-cancel').textContent = '取消撤回';
  byId('delete-cancel').disabled = false;
  void readWithdrawal(removal);
  deleteDialog.showModal();
}

function showWithdrawalStep(removal) {
  const { plan, step } = removal;
  const total = plan.operations.length;
  const button = byId('delete-confirm');
  byId('delete-title').textContent = total ? `确认撤销操作（${step + 1}/${total}）` : '撤回这轮消息？';
  byId('delete-intro').textContent = total
    ? `${plan.operations[step].label}。全部步骤确认后才会开始撤回。`
    : (plan.running ? '将停止本轮处理，并从会话中移除这轮消息。' : '将从会话中移除这轮消息。');
  button.textContent = total ? (step + 1 === total ? '同意并撤回' : '同意此步') : '确认撤回';
  button.disabled = false;
}

async function readWithdrawal(removal) {
  const button = byId('delete-confirm');
  removal.plan = null;
  removal.step = 0;
  byId('delete-title').textContent = '撤回这轮消息？';
  byId('delete-intro').textContent = '正在检查这轮消息执行过的操作…';
  button.textContent = '正在检查…';
  button.disabled = true;
  feedback('delete-feedback');
  try {
    const plan = await api(`/v1/runs/${removal.runId}/withdrawal`);
    if (removal !== deletion || !deleteDialog.open) return;
    removal.plan = plan;
    showWithdrawalStep(removal);
  } catch (err) {
    if (removal !== deletion || !deleteDialog.open) return;
    if (handleChangedTurn(removal, err)) return;
    byId('delete-intro').textContent = '检查完成后才能确认撤回。';
    feedback('delete-feedback', err.message, true);
    button.textContent = '重新检查';
    button.disabled = false;
  }
}

function handleChangedTurn({ conversationId, runId }, err) {
  if (err.status === 404) {
    deleteDialog.close();
    hideWithdrawnTurn(conversationId, runId);
    toast('这轮消息已移除');
  } else if (err.code === 'turn_not_latest') {
    deleteDialog.close();
    toast(err.message);
    const view = currentChatView(conversationId);
    if (view) void recoverRun(view);
  } else return false;
  return true;
}

async function advanceWithdrawal(removal) {
  const { plan, runId, conversationId } = removal;
  if (!plan) return readWithdrawal(removal);
  if (removal.step + 1 < plan.operations.length) {
    removal.step += 1;
    showWithdrawalStep(removal);
    byId('delete-cancel').focus();
    return;
  }
  const button = byId('delete-confirm');
  const cancel = byId('delete-cancel');
  removal.busy = button.disabled = cancel.disabled = true;
  button.textContent = '正在撤回…';
  feedback('delete-feedback');
  try {
    await api(`/v1/runs/${runId}/withdrawal`, {
      method: 'POST', data: { attempt: plan.attempt, approvals: plan.operations.map(item => item.id) },
    });
    if (removal !== deletion || !deleteDialog.open) return;
    deleteDialog.close();
    hideWithdrawnTurn(conversationId, runId);
    campusPanels.refreshActivities();
    toast('这轮消息已撤回');
  } catch (err) {
    if (removal !== deletion || !deleteDialog.open) return;
    if (handleChangedTurn(removal, err)) return;
    removal.plan = null;
    byId('delete-title').textContent = '撤回未完成';
    byId('delete-intro').textContent = '重新检查后，可以确认剩余的撤回步骤。';
    feedback('delete-feedback', err.message, true);
    button.textContent = '重新检查';
  } finally {
    if (removal === deletion) removal.busy = button.disabled = cancel.disabled = false;
  }
}

async function removeSelected() {
  const removal = deletion;
  if (!removal || removal.busy) return;
  if (removal.kind === 'turn') return advanceWithdrawal(removal);
  const { kind, record, generation } = removal;
  const isInstance = kind === 'instance';
  const button = byId('delete-confirm');
  const cancel = byId('delete-cancel');
  removal.busy = button.disabled = cancel.disabled = true;
  button.textContent = '正在删除…';
  feedback('delete-feedback');
  try {
    try {
      await api(`/v1/${isInstance ? 'instances' : 'conversations'}/${record.id}`, { method: 'DELETE' });
    } catch (err) {
      if (err.status !== 404) throw err;
    }
    if (generation !== state.generation) return;
    deleteDialog.close();
    if (isInstance) {
      if (instanceDialog.open) instanceDialog.close();
      state.instances = state.instances.filter(item => item.id !== record.id);
      for (const [id, attempt] of state.pending) {
        if (attempt.instanceId === record.id) state.pending.delete(id);
      }
      refreshSelectedInstance();
      void listInstances();
    } else {
      if (conversationDialog.open) conversationDialog.close();
      state.pending.delete(record.id);
      const space = state.space;
      if (state.view.record?.id === record.id) showView(newView());
      if (space?.instance.id === record.instance_id) {
        space.chats = space.chats.filter(item => item.id !== record.id);
        void listChats(space);
      }
    }
    toast(isInstance ? '实例已删除' : '会话已删除');
  } catch (err) {
    if (removal === deletion) feedback('delete-feedback', err.message, true);
  } finally {
    if (removal === deletion) {
      removal.busy = button.disabled = cancel.disabled = false;
      button.textContent = '永久删除';
    }
  }
}

async function beginCampus(mode, button, password) {
  if (state.authBusy) return;
  state.authBusy = button.disabled = true;
  const feedbackId = mode === 'link' ? 'profile-feedback' : 'auth-feedback';
  feedback(feedbackId);
  try {
    const data = { mode };
    if (mode === 'link') data.current_password = password;
    const result = await api('/v1/auth/sso/campus/start', { method: 'POST', data, guest: mode === 'login' });
    window.location.assign(result.authorization_url);
  } catch (err) {
    feedback(feedbackId, err.message, true);
    state.authBusy = button.disabled = false;
  }
}

function openSettings() {
  if (!byId('close-sidebar').hidden) closeSidebar();
  const user = state.user;
  byId('settings-guest').hidden = Boolean(user);
  byId('settings-account').hidden = !user;
  byId('account-user-id-row').hidden = !user?.id;
  byId('account-user-id').textContent = user?.id || '';
  if (user) {
    byId('account-name').textContent = accountName(user) || '校园助手账号';
    byId('account-email').textContent = user.email;
    feedback('account-feedback');
    byId('campus-account-state').textContent = user.campus_status === 'ready' ? '已绑定校园统一身份' : '需完成校园身份登记';
  }
  if (!settingsDialog.open) settingsDialog.showModal();
  renderMailButtons();
}

async function openProfile() {
  const user = state.user;
  if (!user) return;
  const required = user.campus_status !== 'ready';
  const link = user.campus_status === 'link_required';
  const form = byId('profile-form');
  const generation = state.generation;
  profileDialog.dataset.required = String(required);
  byId('close-profile').hidden = required;
  byId('profile-logout').hidden = !required;
  byId('profile-retry').hidden = true;
  byId('profile-title').textContent = link ? '绑定校园统一身份' : required ? '登记校园个人资料' : '个人资料';
  byId('profile-intro').textContent = link
    ? (state.campus ? '验证当前邮箱密码后，用学工号完成校园统一认证。' : '校园统一认证暂不可用，请联系管理员。')
    : '正在读取个人资料…';
  feedback('profile-feedback');
  byId('campus-link').hidden = !link || !state.campus;
  form.hidden = true;
  if (settingsDialog.open) settingsDialog.close();
  if (!profileDialog.open) profileDialog.showModal();
  if (link) return;
  try {
    const data = await api('/v1/profile');
    if (generation !== state.generation || !profileDialog.open) return;
    if (data.campus_status === 'link_required') {
      await initialize();
      return;
    }
    const teacher = data.role === 'teacher';
    byId('campus-number-label').textContent = teacher ? '工号' : '学号';
    byId('profile-grade-label').hidden = teacher;
    form.elements.grade.required = !teacher;
    form.elements.grade.replaceChildren(new Option('请选择年级', ''),
      ...Object.entries(data.grades).map(([value, label]) => new Option(label, value)));
    form.elements.display_name.value = data.display_name || '';
    for (const name of ['full_name', 'campus_number', 'college', 'grade']) form.elements[name].value = data.profile?.[name] || '';
    const verifiedNumber = data.verified_campus_number;
    form.elements.campus_number.readOnly = Boolean(verifiedNumber);
    form.elements.campus_number.required = !verifiedNumber;
    byId('profile-campus-number-source').hidden = !verifiedNumber;
    if (verifiedNumber) form.elements.campus_number.value = verifiedNumber;
    byId('profile-intro').textContent = data.reviewed
      ? '校园资料已核验。身份资料变更后需要重新核验，称呼可以直接更新。'
      : '请填写真实校园资料。学院和年级经管理员核验后可用于校务准入。';
    form.hidden = false;
  } catch (err) {
    if (generation !== state.generation || !profileDialog.open) return;
    feedback('profile-feedback', err.message, true);
    byId('profile-retry').hidden = false;
  }
}

async function saveProfile(form) {
  const generation = state.generation;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  feedback('profile-feedback');
  try {
    const data = formData(form);
    if (form.elements.campus_number.readOnly) delete data.campus_number;
    data.grade = form.elements.grade.required ? data.grade : null;
    const user = await api('/v1/profile', { method: 'PUT', data });
    if (generation !== state.generation || !state.user) return;
    const enrolling = state.user.campus_status !== 'ready';
    state.user = { ...state.user, ...user };
    hideAvatarGreeting();
    renderAccount();
    profileDialog.close();
    if (enrolling) {
      void listInstances();
      workspaceLayout.scrollIntoView({ block: 'start' });
    } else openSettings();
    toast('个人资料已保存');
  } catch (err) {
    if (generation === state.generation) feedback('profile-feedback', err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function logout(button, feedbackId = 'account-feedback') {
  const generation = state.generation;
  button.disabled = true;
  feedback(feedbackId);
  try {
    await api('/v1/auth/logout', { method: 'POST' });
    if (generation !== state.generation) return;
    replaceUser(null);
    showNotice();
  } catch (err) {
    if (generation === state.generation) feedback(feedbackId, err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function handlePasswordChange(sending) {
  if (!state.user || !passwordDialog.open || passwordOperation) return;
  const form = byId('change-password');
  const key = mailKey('change', state.user.id);
  if (sending && (!form.elements.old_password.reportValidity() || remainingMailTime(key))) return;
  const operation = { sending, generation: state.generation };
  passwordOperation = operation;
  renderMailButtons();
  feedback('password-feedback');
  try {
    const path = sending ? '/v1/auth/password/change/request' : '/v1/auth/password/change';
    const data = sending ? { old_password: form.elements.old_password.value } : formData(form);
    await api(path, { method: 'POST', data });
    if (operation.generation !== state.generation) return;
    if (sending) {
      startMailCooldown(key);
      if (passwordDialog.open) {
        feedback('password-feedback', '请查看账号邮箱，填写本次修改的验证令牌。');
        form.elements.token.focus();
      }
    } else {
      form.reset();
      replaceUser(null);
      openAuth('login', '密码已修改，请重新登录。');
    }
  } catch (err) {
    if (operation.generation !== state.generation) return;
    if (sending && err.code === 'email_cooldown') startMailCooldown(key, err.retryAfter || resendSeconds);
    if (passwordDialog.open) feedback('password-feedback', err.message, true);
  } finally {
    if (passwordOperation === operation) passwordOperation = null;
    renderMailButtons();
  }
}

async function initialize() {
  const generation = state.generation;
  const [providers, account] = await Promise.allSettled([
    api('/v1/auth/providers', { guest: true }),
    api('/v1/me', { guest: true }),
  ]);
  if (providers.status === 'fulfilled') state.campus = providers.value.providers.some(provider => provider.id === 'campus');
  renderAccount();
  if (generation !== state.generation) return;
  if (account.status === 'fulfilled') {
    if (state.user?.websiteContext !== account.value.websiteContext || state.user?.campus_status !== account.value.campus_status) {
      replaceUser(account.value, !state.user);
    }
    if (account.value.campus_status === 'ready') void listInstances();
    else void openProfile();
  }
  if (providers.status === 'rejected' || (account.status === 'rejected' && account.reason.status !== 401)) {
    const err = providers.reason || account.reason;
    showNotice(err.message, { label: '重试', run: () => { showNotice(); void initialize(); } });
  }
}

document.querySelector('.brand').addEventListener('click', () => sidebar(false));
byId('open-sidebar').addEventListener('click', () => {
  setCampusApps(false);
  sidebar(true);
  (byId('new-chat').disabled ? byId('side-settings') : byId('new-chat')).focus();
});
function closeSidebar() {
  sidebar(false);
  byId('open-sidebar').focus();
}
byId('close-sidebar').addEventListener('click', closeSidebar);
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog:modal')) return;
  if (!campusAppsDrawer.inert) {
    event.preventDefault();
    setCampusApps(false, true);
  } else if (!byId('close-sidebar').hidden) {
    closeSidebar();
  }
});
campusAppsToggle.addEventListener('click', () => setCampusApps(campusAppsDrawer.inert));
campusAppsQuery.addEventListener('input', filterCampusApps);
campusAppsQuery.addEventListener('keydown', event => {
  if (event.isComposing) return;
  if (event.key === 'Escape' && campusAppsQuery.value) {
    event.preventDefault();
    campusAppsQuery.value = '';
    filterCampusApps();
  } else if (event.key === 'Enter') {
    const matches = filterCampusApps();
    if (matches.length === 1) openCampusApp(matches[0]);
  }
});
byId('campus-apps-scrim').addEventListener('click', () => setCampusApps(false, true));
compactCampusApps.addEventListener('change', () => setCampusApps(false));
byId('side-settings').addEventListener('click', openSettings);
avatarPreference.addEventListener('change', saveAvatarPreference);
byId('hide-campus-avatar').addEventListener('click', () => {
  avatarPreference.checked = false;
  saveAvatarPreference();
  query.focus({ preventScroll: true });
});
byId('settings-login').addEventListener('click', () => {
  settingsDialog.close();
  openAuth('login');
});
for (const button of campusAppButtons) {
  button.addEventListener('click', () => openCampusApp(button));
}
byId('open-personal-space').addEventListener('click', () => {
  if (!state.user) return;
  settingsDialog.close();
  const restoreFocus = () => {
    const menuButton = byId('open-sidebar');
    const trigger = menuButton.getClientRects().length ? menuButton : byId('side-settings');
    trigger.focus({ preventScroll: true });
  };
  campusPanels.open('semester-report', restoreFocus, null, {
    title: '个人空间',
  });
});
byId('add-instance').addEventListener('click', () => openInstance());
byId('edit-instance').addEventListener('click', () => {
  const instance = state.space?.instance || preferredInstance();
  if (instance) openInstance(instance);
});
byId('instance-select').addEventListener('change', event => {
  const instance = state.instances.find(item => item.id === event.target.value);
  if (instance) chooseInstance(instance);
});
byId('new-chat').addEventListener('click', () => {
  sidebar(false);
  if (!state.user) return openAuth('login');
  if (!state.space) return openInstance();
  showView(newView());
  query.focus();
});
byId('older-chats').addEventListener('click', () => {
  const space = state.space;
  if (space) void listChats(space, space.error ? space.retryOlder : Boolean(space.cursor));
});
historyQuery.addEventListener('input', searchChats);
olderMessages.addEventListener('click', () => void loadOlderMessages());
turnNavOlder.addEventListener('click', () => void loadOlderMessages(true));
bindTurnNavigation();
query.addEventListener('input', () => renderRun(state.view));
query.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    void send();
  }
});
byId('composer').addEventListener('submit', event => { event.preventDefault(); void send(); });
for (const button of document.querySelectorAll('.topic')) {
  button.addEventListener('click', () => {
    query.value = button.dataset.prompt;
    target.value = button.dataset.role;
    renderRun(state.view);
    query.focus();
  });
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}
for (const button of authDialog.querySelectorAll('[data-auth-mode]')) {
  button.addEventListener('click', () => { if (!state.authBusy) openAuth(button.dataset.authMode); });
}
for (const form of authDialog.querySelectorAll('[data-auth-form]')) {
  form.addEventListener('submit', event => { event.preventDefault(); void handleAuth(form); });
}
byId('campus-login').addEventListener('click', event => void beginCampus('login', event.currentTarget));
byId('campus-link').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formData(form);
  form.reset();
  void beginCampus('link', form.querySelector('button'), data.current_password);
});
byId('open-profile').addEventListener('click', () => void openProfile());
byId('profile-retry').addEventListener('click', () => void openProfile());
byId('profile-logout').addEventListener('click', event => void logout(event.currentTarget, 'profile-feedback'));
byId('profile-form').addEventListener('submit', event => { event.preventDefault(); void saveProfile(event.currentTarget); });
profileDialog.addEventListener('cancel', event => { if (profileDialog.dataset.required === 'true') event.preventDefault(); });
profileDialog.addEventListener('close', () => {
  byId('campus-link').reset();
  byId('profile-form').reset();
});
byId('instance-form').addEventListener('submit', event => { event.preventDefault(); void handleInstance(event.currentTarget); });
byId('instance-delete').addEventListener('click', () => confirmDeletion('instance', instanceEditor?.instance));
byId('manage-chat').addEventListener('click', () => openConversation(state.view.record));
byId('conversation-form').addEventListener('submit', event => { event.preventDefault(); void handleConversation(event.currentTarget); });
byId('conversation-delete').addEventListener('click', () => confirmDeletion('conversation', conversationEditor?.chat));
byId('delete-cancel').addEventListener('click', () => deleteDialog.close());
byId('delete-confirm').addEventListener('click', () => void removeSelected());
deleteDialog.addEventListener('cancel', event => { if (deletion?.busy) event.preventDefault(); });
deleteDialog.addEventListener('close', () => { if (!deleteDialog.open) deletion = null; });
byId('verification-email').addEventListener('input', renderMailButtons);
byId('reset-email').addEventListener('input', renderMailButtons);
byId('open-password-change').addEventListener('click', () => {
  if (!state.user || passwordDialog.open) return;
  passwordDialogGeneration = state.generation;
  byId('change-password').reset();
  feedback('password-feedback');
  settingsDialog.close();
  passwordDialog.showModal();
  renderMailButtons();
});
byId('password-change-send').addEventListener('click', () => void handlePasswordChange(true));
byId('change-password').addEventListener('submit', event => {
  event.preventDefault();
  void handlePasswordChange(false);
});
byId('logout').addEventListener('click', event => void logout(event.currentTarget));
authDialog.addEventListener('close', () => {
  loginShortcut = null;
  for (const input of authDialog.querySelectorAll('input[type="password"], input[name="token"]')) input.value = '';
  authDialog.querySelector('input[name=remember_me]').checked = false;
  renderMailButtons();
});
settingsDialog.addEventListener('close', () => {
  for (const form of settingsDialog.querySelectorAll('form')) form.reset();
});
passwordDialog.addEventListener('close', () => {
  if (passwordDialog.open) return;
  const returnToSettings = state.user && passwordDialogGeneration === state.generation;
  passwordDialogGeneration = null;
  byId('change-password').reset();
  feedback('password-feedback');
  if (returnToSettings) {
    void openSettings();
    byId('open-password-change').focus();
  } else {
    renderMailButtons();
  }
});

bindUniversityWatermark();
bindLandingScroll();
bindHeroLens();
try { avatarPreference.checked = localStorage.getItem(avatarStorageKey) === 'shown'; } catch { /* Optional media stays hidden. */ }
syncCampusAvatar();
const hideAvatarGreeting = bindCampusAvatar(campusAvatar, () => state.user);
window.addEventListener('scroll', updateHeroScroll, { passive: true });
window.addEventListener('resize', updateHeroScroll);
updateHeroScroll();
renderRun(state.view);
renderGreeting();
setInterval(renderGreeting, 60_000);
await initialize();
const campusResult = new URLSearchParams(location.search).get('campus');
if (campusResult) {
  history.replaceState(null, '', '/');
  workspaceLayout.scrollIntoView({ block: 'start' });
  if (campusResult === 'linked' && state.user?.campus_status === 'ready') showNotice('校园身份已绑定。');
  if (campusResult === 'failed') {
    if (profileDialog.open) feedback('profile-feedback', '校园认证未完成，请重新发起绑定。', true);
    else openAuth('login', '校园认证未完成，请重新发起登录。', true);
  }
  if (campusResult === 'link-required') {
    if (!profileDialog.open) openAuth('login', '请先用已验证的邮箱账号登录，再绑定校园身份。', true);
  }
}
const todoResult = new URLSearchParams(location.search).get('todo');
if (todoResult) {
  history.replaceState(null, '', '/');
  workspaceLayout.scrollIntoView({ block: 'start', behavior: 'instant' });
  const trigger = campusAppsDrawer.querySelector('[data-campus-panel="calendar-sync"]');
  if (state.user) openCampusApp(trigger);
  if (todoResult === 'connected') showNotice('Microsoft To Do 已连接，正在安排首次同步。');
  else if (todoResult === 'cancelled') showNotice('已取消 Microsoft To Do 授权。');
  else showNotice('Microsoft To Do 授权未完成，请从日历同步页面重新连接。');
}
