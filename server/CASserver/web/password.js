for (const control of document.querySelectorAll('[data-password-control]')) {
  const field = control.querySelector('input');
  const toggle = control.querySelector('button');
  if (!field?.id || !toggle) continue;

  function show(visible) {
    field.type = visible ? 'text' : 'password';
    toggle.textContent = visible ? '隐藏' : '显示';
    toggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  }

  toggle.hidden = false;
  toggle.setAttribute('aria-controls', field.id);
  show(false);
  toggle.addEventListener('click', () => show(field.type === 'password'));
  field.form?.addEventListener('reset', () => show(false));
  field.form?.addEventListener('submit', () => show(false));
  field.closest('dialog')?.addEventListener('close', () => show(false));
  document.addEventListener('visibilitychange', () => { if (document.hidden) show(false); });

  const caps = document.createElement('small');
  caps.className = 'caps-lock-message';
  caps.textContent = '大写锁定已开启';
  caps.setAttribute('role', 'status');
  caps.hidden = true;
  control.after(caps);
  const checkCaps = event => { caps.hidden = !event.getModifierState('CapsLock'); };
  field.addEventListener('keydown', checkCaps);
  field.addEventListener('keyup', checkCaps);
  field.addEventListener('blur', () => { caps.hidden = true; });
}
