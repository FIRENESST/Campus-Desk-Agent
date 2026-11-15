const feedPath = '/v1/campus-rumor';
const labels = { supported: '已核实', refuted: '已辟谣' };

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function makeButton(text, onClick) {
  const button = element('button', 'outline-button', text);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function section(title, className) {
  const block = element('section', className);
  block.append(element('h3', '', title));
  return block;
}

function officialSource(proof) {
  let url;
  try {
    url = new URL(proof.url);
    if (url.protocol !== 'https:' || url.username || url.password) {
      return null;
    }
  } catch {
    return null;
  }
  const wrapper = element('div', 'rumor-source');
  const link = element('a', '', `查看官方原文 · ${proof.published} ↗`);
  link.href = url.href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  wrapper.append(element('blockquote', '', proof.quote), link);
  return wrapper;
}

function feedbackForm(ui, options) {
  const section = element('section', 'rumor-tip');
  const title = element('h3', '', '补充线索');
  const form = element('form', 'rumor-tip-form');
  const label = element('label', '', '你看到的消息或官方出处');
  const text = element('textarea');
  text.name = 'content';
  text.rows = 4;
  text.minLength = 5;
  text.maxLength = 2000;
  text.required = true;
  text.value = options.tipDraft ?? '';
  text.addEventListener('input', () => { options.tipDraft = text.value; });
  label.append(text);
  const note = element('p', 'rumor-meta', '可补充日期、校区和出处链接。反馈交给管理员复核。');
  const submit = element('button', 'primary-button', '提交反馈');
  submit.type = 'submit';
  form.append(label, note, submit);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const content = text.value.trim();
    if (content.length < 5) {
      text.setCustomValidity('请至少填写 5 个字。');
      text.reportValidity();
      text.setCustomValidity('');
      return;
    }
    const result = await ui.write(`${feedPath}/tips`, { method: 'POST', data: { content } }, submit, '反馈已提交，等待复核。');
    if (result?.ok) {
      options.tipDraft = '';
      const field = document.querySelector('#campus-panel[data-panel="rumor"] .rumor-tip-form textarea');
      if (field) field.value = '';
    }
  });
  section.append(title, form);
  return section;
}

export const campusRumorPanel = {
  title: '校园传闻与辟谣',
  intro: '看看近 14 天讨论较多的校园消息，也可以请传传核实一条具体说法。',
  async load(request, _options, signal) {
    const data = await request(feedPath, { signal });
    if (!Array.isArray(data.pending) || !Array.isArray(data.confirmed)) throw new Error('暂时无法读取校园传闻。');
    return data;
  },
  render(data, ui, options) {
    const content = element('div', 'rumor-panel');
    const pending = section('近期热点 · 待核实', 'rumor-section');
    const confirmed = section('官方辟谣与已核实事实', 'rumor-section');
    const sections = element('div', 'rumor-columns');
    if (!data.pending.length) pending.append(element('p', 'rumor-meta', '近 14 天暂无可公开的待核线索。'));
    for (const row of data.pending) {
      const item = element('article', 'rumor-entry');
      item.append(element('span', 'rumor-tag', '待核实'), element('h4', '', row.title),
        element('p', 'rumor-meta', `最后出现 ${row.last_seen} · 讨论热度 ${row.heat}`),
        makeButton('问传传', () => ui.askRumor(row.claim, row.published_on)));
      pending.append(item);
    }
    if (!data.confirmed.length) confirmed.append(element('p', 'rumor-meta', '近 14 天暂无附官方来源的复核结论。'));
    for (const row of data.confirmed) {
      if (!labels[row.verdict]) continue;
      const item = element('article', 'rumor-entry');
      item.append(element('span', 'rumor-tag confirmed', labels[row.verdict]), element('h4', '', row.title),
        element('p', 'rumor-meta', `复核日期 ${row.reviewed_on}`));
      for (const proof of row.sources) {
        const source = officialSource(proof);
        if (source) item.append(source);
      }
      item.append(makeButton('问传传', () => ui.askRumor(row.claim, row.published_on)));
      confirmed.append(item);
    }
    sections.append(pending, confirmed);
    content.append(makeButton('请传传核实一条消息', () => ui.askRumor()), sections, feedbackForm(ui, options));
    return content;
  },
};
