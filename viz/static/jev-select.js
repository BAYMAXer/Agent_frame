/* Accessible, animated single-select controls. Native selects retain form values. */
(() => {
  'use strict';
  const instances = new WeakMap();
  let active = null;
  let sequence = 0;
  const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const chevron = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>';
  const check = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8 3 3 6-6"/></svg>';

  function sync(instance) {
    const option = instance.select.selectedOptions[0];
    instance.text.textContent = option?.textContent || '请选择';
    instance.button.disabled = instance.select.disabled;
  }

  function close({restoreFocus = false, animate = true, notify = true} = {}) {
    if (!active) return;
    const old = active;
    active = null;
    old.instance.button.setAttribute('aria-expanded', 'false');
    old.instance.button.removeAttribute('aria-activedescendant');
    old.instance.wrapper.classList.remove('is-open');
    old.menu.classList.remove('is-open');
    old.menu.setAttribute('aria-hidden', 'true');
    old.menu.style.pointerEvents = 'none';
    if (restoreFocus && old.instance.button.isConnected) old.instance.button.focus({preventScroll: true});
    if (animate && !reducedMotion()) setTimeout(() => old.menu.remove(), 160);
    else old.menu.remove();
    if (notify && old.instance.select.isConnected) {
      queueMicrotask(() => old.instance.select.dispatchEvent(new CustomEvent('select-closed', {bubbles: true})));
    }
  }

  function position() {
    if (!active) return;
    const {instance, menu} = active;
    if (!instance.button.isConnected) {close({animate: false, notify: false}); return;}
    const rect = instance.button.getBoundingClientRect();
    const gap = 8, edge = 12;
    const width = Math.min(Math.max(rect.width, 310), innerWidth - edge * 2);
    const below = innerHeight - rect.bottom - gap - edge;
    const above = rect.top - gap - edge;
    const upwards = below < Math.min(menu.scrollHeight, 260) && above > below;
    const available = Math.max(60, upwards ? above : below);
    menu.style.width = width + 'px';
    menu.style.maxHeight = Math.min(360, available) + 'px';
    menu.style.left = Math.max(edge, Math.min(rect.left, innerWidth - width - edge)) + 'px';
    menu.style.top = (upwards ? rect.top - gap - Math.min(menu.scrollHeight, available, 360) : rect.bottom + gap) + 'px';
    menu.classList.toggle('opens-up', upwards);
  }

  function highlight(index, scroll = true) {
    if (!active) return;
    active.index = Math.max(0, Math.min(index, active.rows.length - 1));
    active.rows.forEach((row, i) => row.classList.toggle('is-active', i === active.index));
    const row = active.rows[active.index];
    if (!row) return;
    active.instance.button.setAttribute('aria-activedescendant', row.id);
    if (scroll) row.scrollIntoView({block: 'nearest'});
  }

  function choose(index) {
    if (!active) return;
    const instance = active.instance;
    const option = active.options[index];
    if (!option || option.disabled) return;
    const changed = instance.select.value !== option.value;
    instance.select.value = option.value;
    sync(instance);
    close({restoreFocus: true});
    if (changed) instance.select.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function open(instance, firstIndex) {
    if (instance.button.disabled) return;
    close({animate: false, notify: false});
    // A rapid reopen must not retain duplicate ARIA IDs from a fading panel.
    document.querySelectorAll('.select-menu[aria-hidden="true"]').forEach(menu => menu.remove());
    sync(instance);
    const menu = document.createElement('div');
    menu.className = 'select-menu';
    menu.id = instance.menuId;
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', instance.label);
    const options = [...instance.select.options].filter(option => !option.hidden);
    const rows = options.map((option, index) => {
      const row = document.createElement('div');
      row.className = 'select-option';
      row.id = instance.menuId + '-option-' + index;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.selected));
      if (option.disabled) row.setAttribute('aria-disabled', 'true');
      const copy = document.createElement('div');
      copy.className = 'select-option-copy';
      const title = document.createElement('span');
      title.className = 'select-option-title';
      title.textContent = option.textContent;
      copy.append(title);
      if (option.dataset.description) {
        const detail = document.createElement('span');
        detail.className = 'select-option-description';
        detail.textContent = option.dataset.description;
        copy.append(detail);
      }
      const tick = document.createElement('span');
      tick.className = 'select-option-check';
      tick.innerHTML = check;
      row.append(copy, tick);
      row.addEventListener('pointermove', () => {if (!option.disabled) highlight(index, false);});
      row.addEventListener('pointerdown', event => event.preventDefault());
      row.addEventListener('click', event => {event.preventDefault();event.stopPropagation();choose(index);});
      menu.append(row);
      return row;
    });
    (instance.select.closest('dialog') || document.body).append(menu);
    active = {instance, menu, options, rows, index: 0};
    instance.wrapper.classList.add('is-open');
    instance.button.setAttribute('aria-expanded', 'true');
    position();
    const selected = options.findIndex(option => option.selected);
    highlight(firstIndex ?? Math.max(0, selected));
    // Two frames ensure both opening and closing opacity/translation animate.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (active?.menu === menu) menu.classList.add('is-open');
    }));
  }

  function keydown(event, instance) {
    const opened = active?.instance === instance;
    if (event.key === 'Escape' && !opened) return;
    if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' ', 'Escape'].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === 'Escape') {if (opened) close({restoreFocus: true}); return;}
      if (event.key === 'Enter' || event.key === ' ') {
        if (opened) choose(active.index); else open(instance);
        return;
      }
      if (!opened) open(instance);
      if (!active) return;
      let index = event.key === 'Home' ? 0 : event.key === 'End' ? active.rows.length - 1 : active.index + (event.key === 'ArrowDown' ? 1 : -1);
      index = Math.max(0, Math.min(index, active.rows.length - 1));
      const direction = event.key === 'ArrowUp' || event.key === 'End' ? -1 : 1;
      while (active.options[index]?.disabled) index += direction;
      if (index >= 0 && index < active.rows.length) highlight(index);
    } else if (event.key === 'Tab' && opened) {
      close();
    } else if (opened && event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
      const index = active.options.findIndex(option => !option.disabled && option.textContent.trim().toLowerCase().startsWith(event.key.toLowerCase()));
      if (index >= 0) highlight(index);
    }
  }

  function enhance(root = document) {
    root.querySelectorAll('select').forEach(select => {
      if (instances.has(select)) {sync(instances.get(select)); return;}
      const wrapper = document.createElement('div');
      wrapper.className = 'select-control';
      wrapper.dataset.select = select.id || select.name || 'select-' + sequence;
      const label = select.getAttribute('aria-label') || [...(select.closest('label')?.childNodes || [])].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim() || '选择选项';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'select-trigger';
      button.id = (select.id || select.name || 'select') + '-trigger';
      button.setAttribute('role', 'combobox');
      button.setAttribute('aria-haspopup', 'listbox');
      button.setAttribute('aria-expanded', 'false');
      button.setAttribute('aria-label', label);
      const menuId = 'decision-select-' + ++sequence;
      button.setAttribute('aria-controls', menuId);
      const text = document.createElement('span');
      text.className = 'select-current';
      const arrow = document.createElement('span');
      arrow.className = 'select-chevron';
      arrow.innerHTML = chevron;
      button.append(text, arrow);
      select.before(wrapper);
      wrapper.append(select, button);
      select.hidden = true;
      select.setAttribute('aria-hidden', 'true');
      select.tabIndex = -1;
      const instance = {select, wrapper, button, text, menuId, label};
      instances.set(select, instance);
      sync(instance);
      button.addEventListener('click', event => {
        event.preventDefault();
        if (active?.instance === instance) close({restoreFocus: true}); else open(instance);
      });
      button.addEventListener('keydown', event => keydown(event, instance));
      select.addEventListener('change', () => sync(instance));
    });
  }

  document.addEventListener('pointerdown', event => {
    if (active && !active.menu.contains(event.target) && !active.instance.wrapper.contains(event.target)) close();
  }, true);
  document.addEventListener('focusin', event => {
    if (active && !active.menu.contains(event.target) && !active.instance.wrapper.contains(event.target)) close();
  });
  document.addEventListener('close', () => close({animate: false, notify: false}), true);
  window.addEventListener('resize', position);
  document.addEventListener('scroll', event => {if (active && !active.menu.contains(event.target)) position();}, true);
  window.DecisionSelect = {
    enhance,
    close,
    isOpenWithin: root => Boolean(active && root?.contains(active.instance.select)),
    isOpen: () => Boolean(active),
    disposeWithin: root => {if (active && root.contains(active.instance.select)) close({animate: false, notify: false});},
  };
})();
