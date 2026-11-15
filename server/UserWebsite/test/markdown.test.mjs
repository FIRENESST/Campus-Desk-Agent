import assert from 'node:assert/strict';
import { test } from 'node:test';

import { renderMarkdown } from '../../../client/UIF/markdown.mjs';

test('renders emphasis, line breaks, lists, code and tables in replies', () => {
  const reply = [
    '156 × 789 = **123,084**',
    'Second line',
    '',
    '## Steps',
    '1. First',
    '2. Use `<value>`',
    '',
    '~~~js',
    'const safe = "<tag>";',
    '~~~',
    '',
    '| Label | Total |',
    '| :--- | ---: |',
    '| Result | 123,084 |',
  ].join('\n');
  const html = renderMarkdown(reply);
  assert.match(html, /156 × 789 = <strong>123,084<\/strong><br>\nSecond line/);
  assert.match(html, /<h2>Steps<\/h2>/);
  assert.match(html, /<ol>\n<li>First<\/li>\n<li>Use <code>&lt;value&gt;<\/code><\/li>/);
  assert.match(html, /<pre><code class="language-js">const safe = &quot;&lt;tag&gt;&quot;;/);
  assert.match(html, /<div class="markdown-table"><table>/);
  assert.match(html, /<td class="markdown-align-right">123,084<\/td>/);
  assert.doesNotMatch(html, /\bstyle=/);
});

test('escapes raw HTML, displays image alt text and limits active link protocols', () => {
  const html = renderMarkdown([
    '<img src=/bad onerror=alert(1)>',
    '<script>alert(1)</script>',
    '![Image & <b>](https://example.invalid/pixel.png)',
    '[Official page](https://example.invalid/docs)',
    '[Local page](/campus.svg)',
  ].join('\n\n'));
  assert.doesNotMatch(html, /<(?:img|script|b)\b/i);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /<p>Image &amp; &lt;b&gt;<\/p>/);
  assert.doesNotMatch(html, /pixel\.png/);
  assert.match(html, /<a href="https:\/\/example\.invalid\/docs" target="_blank" rel="noopener noreferrer">Official page<\/a>/);
  assert.match(html, /<a href="\/campus\.svg" target="_blank" rel="noopener noreferrer">Local page<\/a>/);
  for (const destination of [
    'javascript:alert%281%29', 'jav&#x61;script:alert%281%29', 'data:text/html,hello', 'file:///secret', 'vbscript:alert',
  ]) {
    assert.doesNotMatch(renderMarkdown(`[bad](${destination})`), /<a\b/i, destination);
  }
});
