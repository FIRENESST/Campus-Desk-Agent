const subscriptionsPath = '/v1/calendar/subscriptions';
const exportPath = '/v1/calendar/export.ics';
const todoPath = '/v1/calendar/microsoft-todo';
const androidApkPath = '/downloads/campus-calendar.apk';
const androidQrPath = '/downloads/campus-calendar-qr.svg';
const linkPattern = /^\/calendar\/cal_[A-Za-z0-9_-]{43}\.ics$/;
const createdDate = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeZone: 'Asia/Shanghai' });

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action, className = 'outline-button') {
  const control = element('button', text, className);
  control.type = 'button';
  control.addEventListener('click', action);
  return control;
}

function officialLink(text, url) {
  const link = element('a', text);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function guideSteps(...steps) {
  const list = element('ol', undefined, 'calendar-guide-steps');
  list.append(...steps.map(step => element('li', step)));
  return list;
}

function guideHelp(label, text) {
  const details = element('details', undefined, 'calendar-guide-help');
  details.append(element('summary', label), element('p', text));
  return details;
}

function calendarFeedback() {
  const notice = element('p', '', 'form-feedback');
  notice.hidden = true;
  notice.setAttribute('role', 'status');
  function report(message = '', error = false) {
    notice.hidden = !message;
    notice.textContent = message;
    notice.dataset.tone = error ? 'error' : 'info';
  }
  return { notice, report };
}

function androidDownload() {
  const block = element('div', undefined, 'calendar-android-download');
  const instructions = element('div', undefined, 'calendar-android-instructions');
  const link = element('a', '下载安卓版 APK', 'primary-button');
  link.href = androidApkPath;
  link.download = 'cuc-campus-calendar.apk';
  const details = element('p', 'Android 8.0 及以上 · 安装后同步到系统日历。', 'calendar-guide-note');
  instructions.append(link, details);
  block.append(instructions);
  const hasMobileAddress = window.location.protocol === 'https:' &&
    !['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
  if (hasMobileAddress) {
    const qr = element('img', undefined, 'calendar-download-code');
    qr.hidden = true;
    qr.width = 132;
    qr.height = 132;
    qr.alt = '扫码下载中传校园日程安卓版';
    qr.addEventListener('load', () => { qr.hidden = false; }, { once: true });
    qr.addEventListener('error', () => { details.textContent = '二维码暂不可用，可点击上方按钮下载。'; }, { once: true });
    qr.src = androidQrPath;
    block.append(qr);
  } else {
    instructions.append(element('p', '通过手机可访问的 HTTPS 网站地址打开本页，即可看到下载二维码。', 'calendar-guide-note'));
  }
  return block;
}

function renderCalendar({ items, request, subscriptionsUnavailable, todo }, ui, { panelSignal, fromSchedule }) {
  const panel = element('div', undefined, 'calendar-sync');
  const androidGuide = element('section', undefined, 'calendar-sync-section');
  const { notice, report } = calendarFeedback();
  const androidResources = element('div', undefined, 'calendar-link-controls');
  androidResources.append(
    officialLink('在 F-Droid 免费安装 ICSx⁵', 'https://f-droid.org/packages/at.bitfire.icsdroid/'),
    officialLink('ICSx⁵ 官方说明', 'https://icsx5.bitfire.at/usage/'),
  );
  androidGuide.append(element('h3', '安卓系统日历 · 中传校园日程'), androidDownload(), guideSteps(
    '用手机扫码下载，或在手机上直接点击「下载安卓版 APK」。打开下载的 APK，按手机提示完成安装。',
    '在手机浏览器登录校园助手，从本页创建并复制个人订阅链接。',
    '打开「中传校园日程」，粘贴链接并点击「连接并同步」，按提示允许访问日历。之后可在系统日历里查看。',
  ));
  if (window.location.protocol !== 'https:') {
    androidGuide.append(element('p', '当前页面使用 HTTP。请切换到手机可访问的 HTTPS 地址后，再创建手机订阅。', 'calendar-guide-note'));
  }

  const form = element('form', undefined, 'calendar-create');
  const label = element('label', '订阅名称');
  const name = element('input');
  name.required = true;
  name.maxLength = 60;
  name.placeholder = '例如 我的安卓手机';
  name.autocomplete = 'off';
  label.append(name);
  const create = element('button', '创建链接', 'primary-button');
  create.type = 'submit';
  form.append(label, create);

  const linkBox = element('div', undefined, 'calendar-link-box');
  linkBox.hidden = true;
  const field = element('label', '新订阅链接');
  const input = element('input');
  input.type = 'url';
  input.readOnly = true;
  field.append(input);
  const copy = button('复制链接', async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      if (!panelSignal.aborted) report('链接已复制。');
    } catch {
      if (panelSignal.aborted) return;
      input.focus();
      input.select();
      report('链接已选中，请用系统菜单复制。');
    }
  });
  const linkControls = element('div', undefined, 'calendar-link-controls');
  const android = element('a', '在 ICSx⁵ 打开', 'link-button');
  android.referrerPolicy = 'no-referrer';
  android.hidden = true;
  linkControls.append(copy, android);
  linkBox.append(field, linkControls, element('p', '链接仅在创建时显示，可直接读取你的个人日程。请复制并妥善保管。'));

  const section = element('section', undefined, 'calendar-subscriptions');
  const list = element('ul', undefined, 'calendar-subs-list');
  const empty = element('p', subscriptionsUnavailable ? '暂时无法读取订阅，请稍后刷新。' : '还没有启用的订阅。', 'dialog-intro');
  create.disabled = name.disabled = subscriptionsUnavailable;
  section.append(element('h3', '已启用的订阅'), empty, list);
  const calendarPrivacy = officialLink('查看日程同步的隐私说明', '/privacy#calendar');
  const privacyLink = element('div', undefined, 'calendar-link-controls');
  privacyLink.append(calendarPrivacy);
  let writing = false;
  let newest = null;

  async function change(control, updateFeedback, action) {
    if (writing || panelSignal.aborted) return;
    writing = control.disabled = true;
    ui.setRefreshDisabled(true);
    updateFeedback();
    try {
      await action();
    } catch (error) {
      if (!panelSignal.aborted) updateFeedback(error.message, true);
    } finally {
      writing = control.disabled = false;
      ui.setRefreshDisabled(false);
    }
  }

  function appendSubscription({ id, label: title, created_at: created }) {
    const row = element('li');
    const info = element('div');
    const date = element('time', createdDate.format(new Date(created)));
    date.dateTime = created;
    info.append(element('strong', title), date);
    const revoke = button('撤销', event => {
      if (writing || !window.confirm('撤销后，该链接无法再从校园助手获取更新。继续撤销？')) return;
      void change(event.currentTarget, report, async () => {
        await request(`${subscriptionsPath}/${id}`, { method: 'DELETE', signal: panelSignal });
        if (panelSignal.aborted) return;
        row.remove();
        updateEmpty();
        if (newest === id) {
          newest = null;
          input.value = '';
          android.removeAttribute('href');
          linkBox.hidden = true;
        }
        name.focus({ preventScroll: true });
        report('订阅已撤销。');
      });
    }, 'outline-button calendar-revoke');
    revoke.setAttribute('aria-label', `撤销 ${title}`);
    row.append(info, revoke);
    list.prepend(row);
  }
  function updateEmpty() {
    empty.hidden = list.childElementCount > 0;
  }
  for (const item of [...items].reverse()) appendSubscription(item);
  updateEmpty();

  form.addEventListener('submit', event => {
    event.preventDefault();
    const title = name.value.trim();
    if (!title) { name.focus(); report('请输入订阅名称。', true); return; }
    void change(create, report, async () => {
      const item = await request(subscriptionsPath, { method: 'POST', data: { label: title }, signal: panelSignal });
      if (panelSignal.aborted) return;
      if (typeof item?.path !== 'string' || !linkPattern.test(item.path)) throw new Error('未能读取订阅链接，请刷新后查看。');
      const url = new URL(item.path, window.location.origin);
      newest = item.id;
      input.value = url.href;
      android.hidden = url.protocol !== 'https:';
      if (!android.hidden) android.href = `webcals://${url.host}${url.pathname}`;
      linkBox.hidden = false;
      appendSubscription(item);
      updateEmpty();
      form.reset();
      input.focus({ preventScroll: true });
      input.select();
    });
  });

  androidGuide.append(form, linkBox, notice, section, guideHelp(
    '刷新日历或更换订阅链接',
    '在「中传校园日程」点「立即同步」可手动刷新。手机日历没有显示时，检查对应日历是否已勾选。更换链接时先在安卓应用取消旧订阅，再连接新链接。',
  ));
  const alternative = guideHelp('使用 ICSx⁵ 同步', '安装 ICSx⁵ 后，可点「在 ICSx⁵ 打开」或手动粘贴订阅链接。用户名与密码留空。');
  alternative.append(androidResources);
  androidGuide.append(alternative, privacyLink);

  const fileGuide = element('section', undefined, 'calendar-sync-section');
  const fileFeedback = calendarFeedback();
  const download = button('下载 .ics', event => {
    void change(event.currentTarget, fileFeedback.report, async () => {
      const file = await request(exportPath, { calendar: true, signal: panelSignal });
      if (panelSignal.aborted) return;
      const href = URL.createObjectURL(file);
      const link = element('a');
      link.href = href;
      link.download = 'campus-schedule.ics';
      link.click();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
      fileFeedback.report('日历文件已准备好，可在日历应用中导入。');
    });
  });
  const fileActions = element('div', undefined, 'calendar-link-controls');
  fileActions.append(download);
  fileGuide.append(
    element('h3', '一次性导入 .ics'),
    element('p', '下载后，在支持 .ics 的日历应用中选「导入」，打开 campus-schedule.ics。安卓也可以在 ICSx⁵ 里点「＋」，再选这个文件。', 'calendar-guide-note'),
    element('p', '文件只包含下载时的日程。需要更新时请重新下载。', 'calendar-guide-note'),
    fileActions, fileFeedback.notice,
  );
  panel.append(renderTodo(todo), androidGuide, fileGuide);
  const footer = element('div', undefined, 'calendar-sync-footer');
  if (fromSchedule) {
    footer.append(button('返回个人日程', ui.openSchedule));
    panel.append(footer);
  }
  return panel;

  function renderTodo(data) {
    const card = element('section', undefined, 'calendar-sync-section calendar-todo');
    const status = element('p', undefined, 'calendar-todo-status');
    status.setAttribute('role', 'status');
    const actions = element('div', undefined, 'calendar-link-controls');
    const feedback = calendarFeedback();
    let poll;
    let renderVersion = 0;
    panelSignal.addEventListener('abort', () => clearTimeout(poll), { once: true });

    function watch(current, version, queued) {
      if (!current?.connection || current.connection.sync_error === 'reauthorize') return;
      poll = setTimeout(async () => {
        try {
          const updated = await request(todoPath, { signal: panelSignal });
          if (panelSignal.aborted || !card.isConnected || renderVersion !== version) return;
          if (JSON.stringify(current) === JSON.stringify(updated)) watch(current, version, queued);
          else show(updated);
        } catch {
          // The panel's refresh button can retry status after a network error.
        }
      }, queued || current.connection.running ? 2500 : 8000);
    }

    function show(current, queued = false) {
      clearTimeout(poll);
      watch(current, ++renderVersion, queued);
      actions.replaceChildren();
      if (!current?.configured) {
        status.textContent = current?.unavailable ? '暂时无法读取连接状态，请点右上角「刷新」重试。'
          : 'Microsoft To Do 尚未开通，请联系校园助手管理员。';
        return;
      }
      const connection = current.connection;
      const configuredOrigin = current.redirect_uri ? new URL(current.redirect_uri).origin : null;
      if (!connection || connection.sync_error === 'reauthorize') {
        status.textContent = connection ? '微软授权已失效，请重新连接。'
          : '尚未连接 Microsoft To Do。';
        if (configuredOrigin === location.origin) {
          const connect = button(connection ? '重新授权' : '连接 Microsoft To Do', () => {
            void change(connect, feedback.report, async () => {
              const response = await request(todoPath + '/start', { method: 'POST', data: {}, signal: panelSignal });
              if (!panelSignal.aborted) location.assign(response.authorization_url);
            });
          }, 'primary-button');
          actions.append(connect);
        } else if (configuredOrigin) {
          status.textContent = '请打开已配置的网站，登录校园助手后，再从「日历同步」发起授权。';
          const link = element('a', '打开已配置的网站', 'link-button');
          link.href = configuredOrigin;
          actions.append(link);
        }
      } else {
        const errors = {
          microsoft_throttled: '微软服务限流，稍后自动重试。',
          schedule_unavailable: '暂时无法读取日程，稍后自动重试。',
          campus_identity: '完成校园身份核验后恢复同步。',
          shared_list: '请取消共享「中传校园日程」清单后重试。',
          microsoft_unavailable: '微软服务暂不可用，稍后自动重试。',
        };
        const outcome = queued ? '已安排同步。' : connection.running ? '正在同步…' : errors[connection.sync_error] ||
          (connection.last_synced_at ? `上次检查 ${new Intl.DateTimeFormat('zh-CN', {
            timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
          }).format(new Date(connection.last_synced_at))}` : '等待首次同步。');
        status.textContent = `${connection.account_name} · ${outcome}`;
        const sync = button('立即同步', () => {
          void change(sync, feedback.report, async () => {
            await request(todoPath + '/sync', { method: 'POST', data: {}, signal: panelSignal });
            if (!panelSignal.aborted) show(current, true);
          });
        });
        sync.disabled = connection.running;
        actions.append(sync);
      }
      if (connection) {
        const disconnect = button('断开连接', () => {
          if (!window.confirm('断开后将停止同步。已创建的任务会留在 Microsoft To Do。')) return;
          void change(disconnect, feedback.report, async () => {
            await request(todoPath, { method: 'DELETE', signal: panelSignal });
            if (!panelSignal.aborted) show({ ...current, connection: null });
          });
        });
        actions.append(disconnect);
      }
    }
    const resources = element('div', undefined, 'calendar-link-controls');
    resources.append(officialLink('下载 Microsoft To Do', 'https://www.microsoft.com/zh-cn/microsoft-365/microsoft-to-do-list-app'));
    card.append(
      element('h3', 'Microsoft To Do · Windows 和安卓'),
      status, actions, feedback.notice,
      guideSteps(
        '在 Windows 电脑或安卓手机上安装 Microsoft To Do。多台设备使用同一个微软账号登录。',
        '点「连接 Microsoft To Do」，在微软页面登录同一账号并同意授权。完成后会回到校园助手。',
        '首次同步会自动开始，也可以点「立即同步」。在 To Do 的清单列表中打开「中传校园日程」，点开任务查看起止时间和地点。',
      ),
      resources,
      guideHelp('找不到「中传校园日程」',
        '先确认 To Do 登录的是本页授权的微软账号，再点「立即同步」，等到出现「上次检查」后查看清单。日程通常每 5 分钟检查一次。要改期或取消，请回校园助手操作。'),
    );
    show(data);
    return card;
  }
}

export const calendarSyncPanel = {
  title: '同步到日历',
  intro: '可同步课表或授课安排、已报名活动、教室预约和自定义事件。',
  async load(request, _options, signal) {
    const [subscriptions, todo] = await Promise.all([
      request(subscriptionsPath, { signal }).catch(error => {
        if (error.code !== 'database_unavailable') throw error;
        return { items: [], subscriptionsUnavailable: true };
      }),
      request(todoPath, { signal }).catch(error => {
        if (error.status === 401) throw error;
        return { unavailable: true };
      }),
    ]);
    return { ...subscriptions, todo, request };
  },
  render: renderCalendar,
};
