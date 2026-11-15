import { renderCampusMap } from './campus-map.js';
import { operationPanels } from './campus-operations.js';
import { wechatPanel } from './campus-digest.js';
import { campusRumorPanel } from './campus-rumor.js';
import { schedulePanel } from './campus-schedule.js';
import { calendarSyncPanel } from './campus-calendar.js';
import { semesterReportPanel } from './campus-semester.js';

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Each component owns its data request, controls and rendered DOM.
const components = new Map([
  ['schedule', schedulePanel],
  ['calendar-sync', calendarSyncPanel],
  ['semester-report', semesterReportPanel],
  ['wechat', wechatPanel],
  ['rumor', campusRumorPanel],
  ...operationPanels,
  ['map', {
    title: '校园地图',
    intro: '中国传媒大学北京校区。可切换平面和立体视图、规划步行路线，或选中地标查看照片。',
    async load(request, options, signal) {
      const [base, survey, photos] = await Promise.all([
        request('/campus-map.geojson', { signal }),
        request('/v1/campus-view', { signal }).catch(() => null),
        request('/v1/campus-view/photo-places', { signal }).catch(error => ({ error: error.message })),
      ]);
      return { base, survey, photoPlaces: photos.items || [], photoError: photos.error, request, signal };
    },
    render: data => renderCampusMap(data, element),
  }],
]);

export function createCampusPanels(request, currentUser, askRumor) {
  const byId = id => document.getElementById(id);
  const dialog = byId('campus-panel');
  const title = byId('campus-panel-title');
  const intro = byId('campus-panel-intro');
  const controls = byId('campus-panel-controls');
  const inlineControls = byId('campus-panel-inline-controls');
  const status = byId('campus-panel-status');
  const content = byId('campus-panel-content');
  const refresh = byId('campus-panel-refresh');
  const dismiss = byId('campus-panel-close');
  let active = null;

  function feedback(text, error = false) {
    status.textContent = text;
    status.hidden = !text;
    status.dataset.tone = error ? 'error' : 'info';
  }

  function scheduleRefresh(current) {
    const poll = current.component.poll;
    if (poll) {
      const interval = typeof poll === 'function' ? poll(current.options) : poll;
      current.timer = setTimeout(() => void load(current, true), interval);
    }
  }

  async function load(current, quiet = false, success = '') {
    if (current.writing || active !== current || !dialog.open) return;
    clearTimeout(current.timer);
    current.request?.abort();
    const pending = new AbortController();
    current.request = pending;
    const isCurrent = () => dialog.open && active === current && !pending.signal.aborted;
    const showingLoading = !quiet || content.hasAttribute('aria-busy');
    if (!quiet) {
      feedback('正在读取…');
      content.replaceChildren();
      content.setAttribute('aria-busy', 'true');
      refresh.disabled = true;
    }
    try {
      const data = await current.component.load(request, current.options, pending.signal);
      if (!isCurrent()) return;
      const snapshot = current.component.poll ? JSON.stringify(data) : null;
      if (!quiet || snapshot === null || snapshot !== current.snapshot) {
        const focused = document.activeElement;
        const focusKey = content.contains(focused) && focused.dataset.focusKey;
        const selection = focusKey && typeof focused.selectionStart === 'number'
          ? [focused.selectionStart, focused.selectionEnd, focused.selectionDirection] : null;
        const top = dialog.scrollTop;
        const nestedScroll = new Map(quiet ? Array.from(content.querySelectorAll('[data-preserve-scroll]'),
          region => [region.dataset.preserveScroll, [region.scrollLeft, region.scrollTop]]) : []);
        content.replaceChildren(current.component.render(data, current.ui, current.options));
        current.snapshot = snapshot;
        content.querySelectorAll('[data-preserve-scroll]').forEach(region => {
          const offset = nestedScroll.get(region.dataset.preserveScroll);
          if (offset) [region.scrollLeft, region.scrollTop] = offset;
        });
        if (focusKey) {
          const control = Array.from(content.querySelectorAll('[data-focus-key]'))
            .find(node => node.dataset.focusKey === focusKey);
          (control || refresh).focus({ preventScroll: true });
          if (selection && typeof control?.selectionStart === 'number') control.setSelectionRange(...selection);
        }
        if (quiet) dialog.scrollTop = top;
      }
      if (showingLoading || success || current.refreshError) feedback(success);
      current.refreshError = false;
    } catch (error) {
      if (isCurrent()) {
        feedback(success ? `${success} ${error.message}` : error.message, true);
        current.refreshError = true;
      }
    } finally {
      if (isCurrent()) {
        current.component.update?.(content, current.options);
        content.removeAttribute('aria-busy');
        refresh.disabled = false;
        scheduleRefresh(current);
      }
    }
  }

  async function write(current, path, options, control, success) {
    if (current.writing || active !== current || !dialog.open) return null;
    clearTimeout(current.timer);
    current.request?.abort();
    current.writing = true;
    control.disabled = refresh.disabled = true;
    feedback('正在提交…');
    let result;
    try {
      result = await request(path, options);
    } catch (error) {
      if (active === current) feedback(error.message, true);
    } finally {
      current.writing = false;
      control.disabled = false;
      if (active === current) {
        refresh.disabled = false;
        content.removeAttribute('aria-busy');
      }
    }
    if (active !== current) {
      if (result && current.component.campusActivities) refreshActivities();
      return null;
    }
    if (!result || result.state === 'failed') {
      if (result) feedback(result.message || '操作未完成，请刷新后重试。', true);
      scheduleRefresh(current);
    } else {
      const message = ['pending', 'uncertain', 'closing', 'close_uncertain'].includes(result.state)
        ? '操作结果待确认，请刷新查看。' : success;
      await load(current, true, message);
    }
    return result;
  }

  function clearControls() {
    for (const region of [controls, inlineControls]) {
      region.hidden = true;
      region.replaceChildren();
    }
  }

  function close(restoreFocus = false) {
    if (!active) return;
    const { request: pending, lifetime, focusTrigger, timer } = active;
    active = null;
    clearTimeout(timer);
    pending?.abort();
    lifetime.abort();
    dialog.close();
    delete dialog.dataset.panel;
    clearControls();
    content.replaceChildren();
    content.removeAttribute('aria-busy');
    feedback('');
    if (restoreFocus) focusTrigger?.();
  }

  function open(key, focusTrigger, activityId, { title: panelTitle, fromSchedule = false } = {}) {
    const component = components.get(key);
    if (!component) return;
    clearTimeout(active?.timer);
    active?.request?.abort();
    active?.lifetime.abort();
    const account = currentUser();
    const lifetime = new AbortController();
    const current = { component, focusTrigger, lifetime,
      options: { role: account?.role, userId: account?.id, account, panelSignal: lifetime.signal, activityId, fromSchedule } };
    current.ui = {
      write: (path, options, control, success) => write(current, path, options, control, success),
      reload: () => load(current, true),
      setRefreshDisabled(disabled) {
        if (active === current) refresh.disabled = disabled;
      },
      openActivity: id => open('activities', focusTrigger, id),
      openCalendarSync: () => open('calendar-sync', focusTrigger, null, { fromSchedule: true }),
      openSchedule: () => open('schedule', focusTrigger),
      askRumor: (claim, publishedOn) => {
        close();
        askRumor(claim, publishedOn);
      },
    };
    active = current;
    dialog.dataset.panel = key;
    title.textContent = panelTitle || component.title;
    intro.textContent = component.intro;
    clearControls();
    if (component.controls) {
      const region = component.controlsInline ? inlineControls : controls;
      region.hidden = false;
      component.controls(region, current.options, () => void load(current));
    }
    if (!dialog.open) dialog.show();
    dismiss.focus();
    const initial = load(current);
    if (component.subscribe) {
      void initial.then(() => component.subscribe(request, current.options, current.ui.reload, lifetime.signal));
    }
  }

  function refreshActivities() {
    if (active?.component.campusActivities) void load(active, true);
  }

  refresh.addEventListener('click', () => { if (active) void load(active); });
  dismiss.addEventListener('click', () => close(true));
  dialog.addEventListener('close', () => { if (!dialog.open) close(true); });
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    close(true);
  });
  return { open, close, refreshActivities };
}
