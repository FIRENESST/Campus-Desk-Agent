import { createAvatarGreeting } from './campus-avatar.js';

const still = '/campus.svg';
const media = {
  arrive: '/companion/arrive.gif', reading: '/companion/reading.gif', smile: '/companion/smile.gif',
};
const gestureDuration = 1800;

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

export function bindChatCompanion(dock, composerInput, getUser) {
  const button = dock.querySelector('button');
  const image = button.querySelector('img');
  const bubble = dock.querySelector('.chat-companion-greeting');
  const greeting = createAvatarGreeting(dock, getUser);
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let edge = 'right';
  let heightRatio = .74;
  let drag;
  let ignoreClick = false;
  let conversation;
  let busy = false;
  let playback = 0;
  let phaseTimer;

  function limits() {
    const right = Math.max(0, dock.clientWidth - button.offsetWidth);
    const bottom = Math.max(0, dock.clientHeight - button.offsetHeight);
    const x = Math.min(8, right / 2);
    const y = Math.min(8, bottom / 2);
    return { left: x, right: right - x, top: y, bottom: bottom - y };
  }

  function place(x, y) {
    button.style.setProperty('--companion-x', x + 'px');
    button.style.setProperty('--companion-y', y + 'px');
    if (bubble.hidden) return;

    const opensRight = edge === 'left';
    const adjacent = opensRight ? x + button.offsetWidth + 10 : x - 10;
    const space = opensRight ? dock.clientWidth - adjacent : adjacent;
    bubble.dataset.edge = edge;
    bubble.style.maxInlineSize = Math.min(210, Math.max(20, space - 5)) + 'px';
    const height = bubble.offsetHeight;
    const center = y + button.offsetHeight / 2;
    const remaining = Math.max(0, dock.clientHeight - height);
    const padding = Math.min(4, remaining / 2);
    const top = clamp(center - height / 2, padding, remaining - padding);
    bubble.style.left = (opensRight ? adjacent : adjacent - bubble.offsetWidth) + 'px';
    bubble.style.top = top + 'px';
    bubble.style.setProperty('--greeting-tail-y', clamp(center - top, 8, height - 8) + 'px');
  }

  function dockButton(snap = false) {
    if (dock.hidden || drag) return;
    button.classList.toggle('is-snapping', snap);
    const area = limits();
    place(area[edge], area.top + (area.bottom - area.top) * heightRatio);
  }
  new ResizeObserver(() => dockButton()).observe(dock);

  button.addEventListener('pointerdown', event => {
    if (drag || !event.isPrimary || event.button !== 0) return;
    ignoreClick = false;
    const rect = button.getBoundingClientRect();
    const area = dock.getBoundingClientRect();
    drag = { id: event.pointerId, startX: event.clientX, startY: event.clientY,
      x: rect.left - area.left, y: rect.top - area.top };
    button.setPointerCapture(event.pointerId);
  });
  button.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    if (!drag.moved) greeting.hide();
    drag.moved = true;
    const area = limits();
    drag.position = [clamp(drag.x + dx, area.left, area.right), clamp(drag.y + dy, area.top, area.bottom)];
    button.classList.add('is-dragging');
    place(...drag.position);
  });
  function finishDrag(event) {
    if (!drag || drag.id !== event.pointerId) return;
    const position = drag.position;
    drag = null;
    if (!position) return;
    const [x, y] = position;
    const area = limits();
    edge = x < (area.left + area.right) / 2 ? 'left' : 'right';
    heightRatio = clamp((y - area.top) / Math.max(1, area.bottom - area.top), 0, 1);
    ignoreClick = true;
    button.classList.remove('is-dragging');
    dockButton(true);
  }
  for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    button.addEventListener(event, finishDrag);
  }
  button.addEventListener('click', event => {
    if (dock.hidden) return;
    if (ignoreClick && event.detail !== 0) {
      ignoreClick = false;
      return;
    }
    composerInput.focus({ preventScroll: true });
    greeting.show();
    dockButton();
    void draw('smile');
  });

  async function draw(gesture) {
    clearTimeout(phaseTimer);
    const revision = ++playback;
    if (dock.hidden) return;

    const playGesture = Boolean(gesture) && !motion.matches;
    const resting = busy && !motion.matches ? media.reading : still;
    const source = playGesture ? media[gesture] : resting;
    if (playGesture || image.getAttribute('src') !== source) {
      image.src = source;
    }
    if (!playGesture) return;

    try {
      // Let a newly downloaded gesture display before starting its return timer.
      await image.decode();
    } catch {
      if (revision === playback && image.getAttribute('src') === source) image.src = still;
      return;
    }
    if (revision === playback) phaseTimer = setTimeout(draw, gestureDuration);
  }

  motion.addEventListener('change', () => { void draw(); });
  image.addEventListener('error', () => {
    if (image.getAttribute('src') !== still) image.src = still;
  });

  return (next, working, answered) => {
    const entering = next !== conversation;
    const completed = !entering && busy && !working && answered;
    if (!entering && working === busy) return;
    conversation = next;
    busy = working;
    if (entering) greeting.hide();
    dock.hidden = !next;
    const gesture = working ? undefined : entering ? 'arrive' : completed ? 'smile' : undefined;
    void draw(gesture);
    if (!next) {
      drag = null;
      button.classList.remove('is-dragging');
      return;
    }
    dockButton();
  };
}
