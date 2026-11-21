const feedPath = '/v1/campus-digest/wechat';
const followingPath = `${feedPath}/following`;
const accountPageSize = 24;
const defaultRefreshSeconds = 5;
const number = new Intl.NumberFormat('zh-CN');
const syncTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
});
const articleDateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function button(text, action) {
  const item = element('button', 'outline-button', text);
  item.type = 'button';
  item.addEventListener('click', action);
  return item;
}

function searchField(name, labelText, placeholder, maxLength) {
  const label = element('label', '', labelText);
  const input = element('input');
  input.name = name;
  input.type = 'search';
  input.maxLength = maxLength;
  input.placeholder = placeholder;
  label.append(input);
  return label;
}

function searchControls(controls, options, reload) {
  Object.assign(options, { title: '', account: '', offset: 0 });
  const form = element('form', 'digest-search');
  const titleField = searchField('title', '文章标题', '输入标题关键词', 200);
  const accountField = searchField('account', '公众号名称', '输入公众号名称', 120);
  const submit = element('button', 'primary-button', '搜索');
  submit.type = 'submit';
  const apply = () => {
    const fields = new FormData(form);
    const title = fields.get('title').trim();
    const account = fields.get('account').trim();
    if (title === options.title && account === options.account && !options.offset) return;
    Object.assign(options, { title, account, offset: 0 });
    reload();
  };
  const clear = button('清除', () => {
    form.reset();
    apply();
    form.elements.namedItem('title').focus();
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    apply();
  });
  const actions = element('div', 'digest-search-actions');
  actions.append(submit, clear);
  form.append(titleField, accountField, actions);
  controls.append(form);
}

function articleLink(value) {
  try {
    const url = new URL(value);
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch { /* This source may only supply an article identifier. */ }
  return null;
}

function wechatSearch(query, article = false) {
  return `https://weixin.sogou.com/weixin?${new URLSearchParams({ type: article ? '2' : '1', query })}`;
}

function externalLink(text, href, label) {
  const link = element('a', '', text);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  if (label) link.setAttribute('aria-label', label);
  return link;
}

function articleTime(article) {
  const timestamp = article.published_at;
  if (typeof timestamp === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(timestamp)) {
    const local = /(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp) ? timestamp : `${timestamp}+08:00`;
    const date = new Date(local);
    if (Number.isFinite(date.getTime())) {
      const time = element('time', 'digest-published', `发布于 ${articleDateTime.format(date)}`);
      time.dateTime = local;
      return time;
    }
  }
  if (article.published_on) {
    const time = element('time', 'digest-published', `发布于 ${article.published_on}（仅有日期）`);
    time.dateTime = article.published_on;
    return time;
  }
  return element('span', 'digest-published', '发布时间未提供');
}

function followButton(account, followed, ui, options) {
  const control = button(followed ? '已特别关注' : '特别关注', () => {
    options.offset = 0;
    void ui.write(followingPath, { method: 'PUT', data: { account_id: account.id, followed: !followed } },
      control, followed ? `已取消特别关注 ${account.name}。` : `已特别关注 ${account.name}。`);
  });
  control.classList.add('digest-follow-button');
  control.setAttribute('aria-pressed', String(followed));
  control.setAttribute('aria-label', `${followed ? '取消特别关注' : '特别关注'} ${account.name}`);
  control.dataset.focusKey = `${options.showFollowing ? 'account' : 'article'}-follow-${account.id}`;
  return control;
}

function articleCard(article, ui, options, followingEnabled) {
  const item = element('li', 'digest-article');
  item.classList.toggle('digest-article-followed', article.followed === true);
  const meta = element('div', 'digest-article-meta');
  const publisher = element('strong', '', article.publisher || '微信公众号');
  if (article.publisher) publisher.replaceChildren(
    externalLink(article.publisher, wechatSearch(article.publisher), `查找公众号 ${article.publisher}`),
  );
  meta.append(publisher, articleTime(article));
  const title = element('h3');
  const href = articleLink(article.url);
  if (href) title.append(externalLink(article.title, href));
  else title.textContent = article.title;
  item.append(meta, title);
  if (article.excerpt) item.append(element('p', 'digest-article-excerpt', article.excerpt));
  if (article.registration_deadline) {
    item.append(element('p', 'digest-deadline', `日报提取的报名截止时间 ${article.registration_deadline}`));
  }
  const links = element('div', 'digest-article-links');
  links.append(externalLink(href ? '打开原文 ↗' : '搜索原文 ↗',
    href || wechatSearch(article.title, true), `${href ? '打开' : '搜索'}原文 ${article.title}`));
  if (followingEnabled && article.account_id && article.publisher) {
    const toggle = followButton({ id: article.account_id, name: article.publisher }, article.followed === true, ui, options);
    toggle.dataset.focusKey += `-${article.id}`;
    links.append(toggle);
  }
  item.append(links);
  return item;
}

function renderFollowDirectory(data, ui, options) {
  const area = element('section', 'digest-follow-directory');
  area.id = 'digest-follow-directory';
  const heading = element('h3', '', '选择要特别关注的公众号');
  const controls = element('form', 'digest-follow-search');
  const search = searchField('publisher', '搜索公众号', '输入公众号名称', 120);
  const field = search.querySelector('input');
  field.value = options.followQuery || '';
  field.dataset.focusKey = 'digest-follow-q';
  field.addEventListener('input', () => { options.followDraft = field.value; });
  if (options.followDraft !== undefined) field.value = options.followDraft;
  const apply = element('button', 'outline-button', '筛选');
  apply.type = 'submit';
  controls.append(search, apply);
  controls.addEventListener('submit', event => {
    event.preventDefault();
    options.followQuery = field.value.trim();
    options.followOffset = 0;
    void ui.reload();
  });
  area.append(heading, controls);
  const followed = new Set(data.following);
  if (data.directory.accounts.length) {
    const list = element('ul', 'digest-follow-list');
    list.dataset.preserveScroll = 'digest-follow-list';
    for (const account of data.directory.accounts) {
      const row = element('li', 'digest-follow-row');
      row.append(element('span', '', account.name), followButton(account, followed.has(account.id), ui, options));
      list.append(row);
    }
    area.append(list);
  } else {
    area.append(element('p', 'digest-empty', '没有找到符合条件的公众号。'));
  }
  const offset = options.followOffset || 0;
  if (offset || data.directory.has_more) {
    const nav = element('div', 'digest-pagination');
    const move = change => {
      options.followOffset = offset + change * accountPageSize;
      void ui.reload();
    };
    const previous = button('上一页公众号', () => move(-1));
    const next = button('下一页公众号', () => move(1));
    previous.disabled = offset === 0;
    next.disabled = !data.directory.has_more;
    previous.dataset.focusKey = 'digest-follow-previous';
    next.dataset.focusKey = 'digest-follow-next';
    nav.append(previous, element('span', '', `第 ${number.format(offset / accountPageSize + 1)} 页`), next);
    area.append(nav);
  }
  return area;
}

function renderFeed(data, ui, options) {
  const feed = element('div', 'digest-feed');
  const updated = new Date(data.revision.updated_at);
  if (Number.isFinite(updated.getTime())) {
    const label = data.stale ? '上次成功更新' : '内容更新于';
    feed.append(element('p', 'digest-sync', `${label} ${syncTime.format(updated)}`));
  }
  if (data.collection_paused) {
    feed.append(element('p', 'digest-sync', '实时采集暂不可用，当前显示已缓存的文章。'));
  }
  if (data.following_enabled) {
    const toolbar = element('div', 'digest-follow-toolbar');
    const toggle = button(options.showFollowing ? '收起公众号名录' : '管理特别关注', () => {
      options.showFollowing = !options.showFollowing;
      void ui.reload();
    });
    toggle.setAttribute('aria-expanded', String(Boolean(options.showFollowing)));
    toggle.setAttribute('aria-controls', 'digest-follow-directory');
    toggle.dataset.focusKey = 'digest-follow-manager';
    toolbar.append(element('span', '', `特别关注 ${number.format(data.following.length)} 个公众号`), toggle);
    feed.append(toolbar);
    if (options.showFollowing && data.directory) feed.append(renderFollowDirectory(data, ui, options));
  }
  if (data.items.length) {
    const list = element('ul', 'digest-articles');
    list.append(...data.items.map(article => articleCard(article, ui, options, data.following_enabled)));
    feed.append(list);
  } else {
    feed.append(element('p', 'digest-empty', options.title || options.account
      ? '没有找到符合搜索条件的公众号文章。' : '校园日报暂未收录公众号文章。'));
  }
  if (options.offset || data.has_more) {
    const nav = element('div', 'digest-pagination');
    const offset = options.offset;
    const pageSize = data.page_size;
    const move = offset => {
      options.offset = offset;
      void ui.reload();
    };
    const previous = button('上一页', () => move(offset - pageSize));
    const next = button('下一页', () => move(offset + pageSize));
    previous.disabled = offset === 0;
    next.disabled = !data.has_more;
    previous.dataset.focusKey = 'digest-previous';
    next.dataset.focusKey = 'digest-next';
    nav.append(previous, element('span', '', `第 ${number.format(offset / pageSize + 1)} 页`), next);
    feed.append(nav);
  }
  return feed;
}

function pause(signal, ms) {
  return new Promise(resolve => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
  });
}

async function subscribe(request, options, reload, signal) {
  let retry = 1000;
  while (!signal.aborted) {
    try {
      const stream = await request(`${feedPath}/events`, { events: true, signal });
      let buffer = '';
      for await (const chunk of stream.pipeThrough(new TextDecoderStream())) {
        buffer += chunk;
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const event = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const match = /^data: (\d+)$/.exec(event);
          if (!match || signal.aborted) continue;
          retry = 1000;
          const version = Number(match[1]);
          if (version !== options.version && (version || options.version !== undefined)) void reload();
        }
        if (buffer.length > 1024) throw new Error('校园日报更新消息无效。');
      }
    } catch (error) {
      if (error.status === 401 || error.status === 403) return;
    }
    await pause(signal, retry);
    retry = Math.min(retry * 2, 15_000);
  }
}

export const wechatPanel = {
  title: '公众号总成',
  intro: '微信读书每 5 分钟轮询一次。发布时间为北京时间。',
  controls: searchControls,
  controlsInline: true,
  poll: options => (options.refreshSeconds ?? defaultRefreshSeconds) * 1000,
  async load(request, options, signal) {
    const query = new URLSearchParams({ offset: options.offset });
    if (options.account) query.set('account', options.account);
    if (options.title) query.set('title', options.title);
    const directoryQuery = new URLSearchParams({ q: options.followQuery || '', limit: accountPageSize, offset: options.followOffset || 0 });
    const [data, directory] = await Promise.all([
      request(`${followingPath}?${query}`, { signal }),
      options.showFollowing ? request(`${feedPath}/accounts?${directoryQuery}`, { signal }) : null,
    ]);
    if (!data?.revision || !Number.isSafeInteger(data.revision.version) || !Array.isArray(data.items) ||
        !Array.isArray(data.following) || directory && !Array.isArray(directory.accounts) ||
        !Number.isSafeInteger(data.page_size) || data.page_size < 1) {
      throw new Error('无法读取公众号文章，请刷新。');
    }
    if (!signal.aborted) {
      options.version = data.revision.version;
      options.refreshSeconds = Number.isSafeInteger(data.refresh_seconds) && data.refresh_seconds > 0
        ? data.refresh_seconds : defaultRefreshSeconds;
    }
    return { ...data, directory };
  },
  render: renderFeed,
  subscribe,
};
