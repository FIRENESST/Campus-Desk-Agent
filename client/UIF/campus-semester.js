const activityDate = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const statusText = {
  restricted: '当前身份无法查看这部分数据。',
  unverified: '核验教职工工号后即可查看授课结算。',
  unavailable: '暂时无法读取这部分数据，请稍后刷新。',
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function points(value) {
  return value === null ? '—' : `${value} 分`;
}

function metric(label, value, total) {
  const box = element('div', undefined, total ? 'semester-total' : '');
  box.append(element('dt', label), element('dd', points(value)));
  return box;
}

function breakdown(title, data, headings, values, empty) {
  const section = element('section', undefined, 'semester-section');
  const heading = element('h3', title);
  section.append(heading);
  if (data.status !== 'ok' || !data.items.length) {
    section.append(element('p', statusText[data.status] || empty, 'semester-note'));
    return section;
  }

  const table = element('table');
  table.createCaption().textContent = title;
  table.caption.className = 'sr-only';
  const head = table.createTHead().insertRow();
  for (const label of headings) {
    const cell = element('th', label);
    cell.scope = 'col';
    head.append(cell);
  }
  const body = table.createTBody();
  for (const item of data.items) {
    const [name, ...columns] = values(item);
    const row = body.insertRow();
    const label = element('th', name);
    label.scope = 'row';
    row.append(label, ...columns.map(value => element('td', value)));
  }
  section.append(table);
  return section;
}

export const semesterReportPanel = {
  title: '学期报告',
  intro: '成绩为百分制模拟数据。每门课程和每项活动各计 1–4 分，活动积分按本人已确认的报名更新。',
  campusActivities: true,
  poll: 15_000,
  async load(request, _options, signal) {
    return request('/v1/minta/semester-report', { signal });
  },
  render({ term, audience, courses, activities, total_points: total }) {
    const panel = element('div', undefined, 'semester-report');
    const header = element('header', undefined, 'semester-term');
    header.append(element('h3', term.name));
    if (term.starts_on && term.ends_on) header.append(element('p', `${term.starts_on} 至 ${term.ends_on}`));

    const totals = element('dl', undefined, 'semester-totals');
    totals.append(metric('积分总和', total, true), metric('课程积分', courses.points), metric('活动积分', activities.points));
    const teaching = audience === 'teacher';
    panel.append(header, totals,
      breakdown(teaching ? '授课模拟结算' : '课程成绩结算', courses, ['课程', '模拟成绩', '积分'],
        ({ course_name, grade, points: score }) => [course_name, points(grade), points(score)],
        teaching ? '本学期暂无授课记录。' : '本学期暂无课程记录。'),
      breakdown('报名活动', activities, ['活动', '开始时间', '积分'],
        ({ name, starts_at, points: score }) => [name, activityDate.format(new Date(starts_at)), points(score)],
        '本学期暂无已确认的活动报名。'));
    return panel;
  },
};
