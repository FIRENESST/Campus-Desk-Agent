import MarkdownIt from './vendor/markdown-it-15.0.3.mjs';

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true });
const allowedProtocols = new Set(['http:', 'https:', 'mailto:']);

markdown.validateLink = url => {
  try {
    return allowedProtocols.has(new URL(url, 'https://markdown.invalid/').protocol);
  } catch {
    return false;
  }
};

markdown.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  const link = tokens[index];
  link.attrSet('target', '_blank');
  link.attrSet('rel', 'noopener noreferrer');
  return renderer.renderToken(tokens, index, options);
};

markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const alt = renderer.renderInlineAsText(tokens[index].children ?? [], options, env);
  return markdown.utils.escapeHtml(alt);
};

markdown.renderer.rules.table_open = () => '<div class="markdown-table"><table>\n';
markdown.renderer.rules.table_close = () => '</table></div>\n';
for (const tag of ['th_open', 'td_open']) {
  markdown.renderer.rules[tag] = (tokens, index, options, _env, renderer) => {
    const cell = tokens[index];
    const styleIndex = cell.attrIndex('style');
    if (styleIndex !== -1) {
      const [[, style]] = cell.attrs.splice(styleIndex, 1);
      const alignment = /^text-align:(left|center|right)$/.exec(style)?.[1];
      if (alignment) cell.attrSet('class', `markdown-align-${alignment}`);
    }
    return renderer.renderToken(tokens, index, options);
  };
}

export function renderMarkdown(content) {
  return markdown.render(content);
}
