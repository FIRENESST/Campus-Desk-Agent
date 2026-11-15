const form = document.querySelector('#cas-import');

form?.addEventListener('submit', async event => {
  event.preventDefault();
  const file = form.elements.file.files[0];
  const status = form.querySelector('output');
  const button = form.querySelector('button');
  const format = file?.name.split('.').at(-1)?.toLowerCase();
  if (!['csv', 'xlsx'].includes(format) || file.size > 256 * 1024) {
    status.textContent = '请选择不超过 256 KiB 的 .xlsx 或 .csv 表格。';
    return;
  }
  button.disabled = true;
  status.textContent = '正在验证表格并创建账号…';
  try {
    const reply = await fetch('/admin/import?format=' + format, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await reply.json();
    if (reply.ok) location.assign('/admin?updated=import');
    else status.textContent = data.message || '导入失败。';
  } catch {
    status.textContent = '暂时无法确认导入结果，请刷新后查看。';
  } finally {
    button.disabled = false;
  }
});

for (const account of document.querySelectorAll('[data-password-form]')) {
  const { password, confirmation } = account.elements;
  const verify = () => {
    password.setCustomValidity(password.value && !(/(?=.*[a-z])(?=.*[0-9])/.test(password.value))
      ? '密码需包含数字和小写英文字母。' : '');
    confirmation.setCustomValidity(confirmation.value && confirmation.value !== password.value
      ? '两次输入的密码不一致。' : '');
  };
  account.addEventListener('input', verify);
  account.addEventListener('submit', event => {
    verify();
    if (!account.reportValidity()) event.preventDefault();
  });
}

const logList = document.querySelector('#cas-log');
const logState = document.querySelector('#log-state');
const logLevels = new Map([['info', '信息'], ['warning', '提醒'], ['error', '错误']]);
const logTime = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
let lastLog = '';

function renderLog(items) {
  const records = JSON.stringify(items);
  if (records === lastLog) return;
  lastLog = records;
  const position = logList.scrollTop;
  const follow = logList.scrollHeight - logList.clientHeight - position < 12;
  const rows = items.map(item => {
    const row = document.createElement('li');
    const time = document.createElement('time');
    const level = document.createElement('span');
    const message = document.createElement('span');
    time.dateTime = item.at;
    time.textContent = logTime.format(new Date(item.at));
    level.className = item.level;
    level.textContent = logLevels.get(item.level) || '信息';
    message.textContent = item.message;
    row.append(time, level, message);
    return row;
  });
  logList.replaceChildren(...rows);
  logList.scrollTop = follow ? logList.scrollHeight : position;
}

async function refreshLog() {
  if (!document.hidden) {
    try {
      const reply = await fetch('/admin/logs', { cache: 'no-store', signal: AbortSignal.timeout(4000) });
      if (reply.status === 401) {
        logState.textContent = '管理会话已过期，请重新登录';
        logList.replaceChildren();
        return;
      }
      if (!reply.ok) throw new Error();
      renderLog((await reply.json()).items);
      logState.textContent = '每 2 秒刷新';
    } catch {
      logState.textContent = '连接暂不可用，正在重试';
    }
  }
  setTimeout(refreshLog, 2000);
}

if (logList) void refreshLog();
