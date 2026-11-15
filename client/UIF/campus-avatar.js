const assetRoot = '/live2d/';
const bundles = ['pixi.min.js', 'unsafe-eval.min.js', 'live2dcubismcore.min.js', 'cubism4.min.js'];
const greetingMotions = ['Nod', 'Shake'];
const compoundSurnames = new Set([
  '欧阳', '司马', '上官', '诸葛', '夏侯', '皇甫', '尉迟', '公孙', '长孙', '慕容',
  '令狐', '端木', '宇文', '独孤', '南宫', '东方', '司徒', '司空', '闻人', '百里',
  '钟离', '申屠', '呼延', '赫连', '万俟', '第五', '澹台', '拓跋', '轩辕', '西门',
  '歐陽', '司馬', '諸葛', '尉遲', '公孫', '長孫', '獨孤', '南宮', '東方',
  '聞人', '鍾離', '鐘離', '赫連', '萬俟', '軒轅', '西門',
]);
const greetings = {
  student: [
    '同学，你好呀！',
    '同学，见到你真好。',
    '同学，欢迎回来！',
    '同学，今天想聊点什么？',
    '同学，一起逛逛校园吧！',
  ],
  teacher: [
    '',
    '欢迎回来。',
    '很高兴见到您。',
    '今天想了解些什么？',
    '有什么我可以帮您？',
  ],
  guest: [
    '你好呀，很高兴见到你！',
    '见到你真好，我是传传。',
    '欢迎来找传传！',
    '今天想聊点什么？',
    '要一起逛逛校园吗？',
  ],
};

function teacherSurname(fullName) {
  const name = fullName?.replace(/\s/g, '') || '';
  if (!/^\p{Script=Han}+$/u.test(name)) return '';
  const [first, second = '', third] = name;
  const pair = first + second;
  return third && compoundSurnames.has(pair) ? pair : first;
}

export function createAvatarGreeting(stage, getUser) {
  const bubble = stage.querySelector('.avatar-greeting');
  const text = bubble.querySelector('.avatar-greeting-text');
  const status = stage.querySelector('.avatar-greeting-status');
  let lastGreeting;
  let timer;

  function hide() {
    clearTimeout(timer);
    bubble.hidden = true;
    text.textContent = status.textContent = '';
  }

  function show() {
    const user = getUser();
    const role = user?.role;
    const variants = Object.hasOwn(greetings, role) ? greetings[role] : greetings.guest;
    const choices = variants.filter(greeting => greeting !== lastGreeting);
    lastGreeting = choices[Math.floor(Math.random() * choices.length)];
    const greeting = role === 'teacher' ? `${teacherSurname(user.full_name)}老师，您好！${lastGreeting}` : lastGreeting;
    text.textContent = status.textContent = greeting;
    bubble.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(hide, 4500);
  }

  return { show, hide };
}

function loadBundle(file) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = assetRoot + 'vendor/' + file;
    script.addEventListener('load', resolve, { once: true });
    script.addEventListener('error', () => reject(new Error('Unable to load ' + file)), { once: true });
    document.head.append(script);
  });
}

function contentBounds(internal) {
  const core = internal.coreModel;
  core.update();
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (let index = 0; index < core.getDrawableCount(); index += 1) {
    const vertices = internal.getDrawableVertices(index);
    for (let offset = 0; offset < vertices.length; offset += 2) {
      left = Math.min(left, vertices[offset]);
      right = Math.max(right, vertices[offset]);
      top = Math.min(top, vertices[offset + 1]);
      bottom = Math.max(bottom, vertices[offset + 1]);
    }
  }
  return right > left && bottom > top ? { left, top, right, bottom }
    : { left: 0, top: 0, right: internal.originalWidth, bottom: internal.originalHeight };
}

function gazeOffset(position, center, viewportSize) {
  const offset = position - center;
  const distance = offset < 0 ? center : viewportSize - center;
  return Math.max(-1, Math.min(1, offset / Math.max(1, distance)));
}

async function createAvatar(figure, canvas) {
  for (const bundle of bundles) await loadBundle(bundle);
  const { Application, live2d } = window.PIXI;
  live2d.config.sound = false;
  const app = new Application({
    view: canvas, width: 1, height: 1, backgroundAlpha: 0,
    autoStart: false, autoDensity: true, antialias: true,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
  });

  try {
    const model = await live2d.Live2DModel.from(assetRoot + 'model/seethrough_output_1.model3.json', {
      autoUpdate: false, autoInteract: false, motionPreload: live2d.MotionPreloadStrategy.ALL,
    });
    app.stage.addChild(model);
    const { left, top, right, bottom } = contentBounds(model.internalModel);

    function fit() {
      const width = figure.clientWidth;
      const height = figure.clientHeight;
      if (!width || !height) return;
      const portrait = getComputedStyle(figure).getPropertyValue('--avatar-fit').trim() === 'portrait';
      const frameHeight = height * (portrait ? 2.3 : .94);
      const scale = Math.min(width * .92 / (right - left), frameHeight / (bottom - top));
      const y = portrait ? height * .045 - top * scale : height * .52 - (top + bottom) * scale / 2;
      app.renderer.resize(width, height);
      model.scale.set(scale);
      model.position.set(width / 2 - (left + right) * scale / 2, y);
      if (!app.ticker.started) app.render();
    }

    fit();
    new ResizeObserver(fit).observe(figure);

    let pointer = null;
    let gazeX = 0, gazeY = 0;
    function trackPointer(event) {
      if (event.pointerType === 'mouse') pointer = { x: event.clientX, y: event.clientY };
    }
    const clearPointer = () => { pointer = null; };
    const core = model.internalModel.coreModel;
    const bodyAngleY = core.getParameterIndex('ParamBodyAngleY');
    const standingBodyAngleY = core.getParameterDefaultValue(bodyAngleY);
    model.internalModel.on('beforeModelUpdate', () => {
      // In the original rig, body pitch also compresses the leg mesh.
      core.setParameterValueByIndex(bodyAngleY, standingBodyAngleY);
      core.addParameterValueById('ParamAngleX', gazeX * 6);
      core.addParameterValueById('ParamAngleY', gazeY * 4);
      core.addParameterValueById('ParamEyeBallX', gazeX * .22);
      core.addParameterValueById('ParamEyeBallY', gazeY * .18);
    });
    app.ticker.maxFPS = 30;
    app.ticker.add(() => {
      const elapsed = Math.min(app.ticker.deltaMS, 50);
      const smoothing = 1 - Math.exp(-elapsed / 120);
      let targetX = 0, targetY = 0;
      if (pointer) {
        const bounds = figure.getBoundingClientRect();
        targetX = gazeOffset(pointer.x, bounds.left + bounds.width / 2, window.innerWidth);
        targetY = -gazeOffset(pointer.y, bounds.top + bounds.height / 2, window.innerHeight);
      }
      gazeX += (targetX - gazeX) * smoothing;
      gazeY += (targetY - gazeY) * smoothing;
      model.update(elapsed);
    });

    let lastMotion;
    return {
      setPlaying(playing) {
        if (playing) {
          window.addEventListener('pointermove', trackPointer, { capture: true, passive: true });
          document.documentElement.addEventListener('pointerleave', clearPointer);
          window.addEventListener('blur', clearPointer);
          app.start();
        } else {
          window.removeEventListener('pointermove', trackPointer, true);
          document.documentElement.removeEventListener('pointerleave', clearPointer);
          window.removeEventListener('blur', clearPointer);
          clearPointer();
          app.stop();
        }
      },
      async greet() {
        const choices = greetingMotions.filter(name => name !== lastMotion);
        lastMotion = choices[Math.floor(Math.random() * choices.length)];
        await model.motion(lastMotion, 0, live2d.MotionPriority.FORCE);
      },
    };
  } catch (error) {
    app.destroy(false, { children: true, texture: true, baseTexture: true });
    throw error;
  }
}

export function bindCampusAvatar(stage, getUser) {
  const figure = stage.querySelector('.avatar-figure');
  const canvas = stage.querySelector('.avatar-model');
  const greeting = createAvatarGreeting(stage, getUser);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let inView = false;
  let requested = false;
  let avatar;

  const active = () => inView && !stage.hidden && !document.hidden;
  const endEntrance = () => stage.classList.remove('is-entering');
  canvas.addEventListener('animationend', endEntrance);

  figure.addEventListener('click', () => {
    if (!avatar || !active()) return;
    greeting.show();
    if (!reducedMotion.matches) {
      avatar.greet().catch(error => console.warn('Live2D greeting motion unavailable:', error));
    }
  });

  async function sync() {
    if (active() && !requested) {
      requested = true;
      try {
        avatar = await createAvatar(figure, canvas);
      } catch (error) {
        stage.dataset.state = 'unavailable';
        stage.hidden = true;
        observer.disconnect();
        console.warn('Live2D avatar unavailable:', error);
      }
    }
    if (!avatar) return;
    const visible = active();
    const animate = visible && !reducedMotion.matches;
    figure.disabled = !visible;
    if (!visible) greeting.hide();
    if (visible && !stage.classList.contains('is-present')) {
      stage.classList.toggle('is-entering', animate);
      stage.classList.add('is-present');
    }
    if (!animate) endEntrance();
    avatar.setPlaying(animate);
  }

  const observer = new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting && entry.intersectionRatio >= .55;
    void sync();
  }, { threshold: .55 });
  observer.observe(stage);
  new MutationObserver(() => {
    if (!stage.hidden) return;
    inView = false;
    void sync();
  }).observe(stage, { attributes: true, attributeFilter: ['hidden'] });
  document.addEventListener('visibilitychange', sync);
  reducedMotion.addEventListener('change', sync);

  return greeting.hide;
}
