/* Presentation only: observe existing selection state; never replace a control. */
(() => {
  'use strict';

  function slidingIndicator(container, buttonSelector, className, describe) {
    if (!container) return;
    const buttons = Array.from(container.querySelectorAll(buttonSelector));
    if (!buttons.length) return;
    const indicator = document.createElement('span');
    indicator.className = className;
    indicator.setAttribute('aria-hidden', 'true');
    container.append(indicator);
    let frame = 0;

    function update() {
      frame = 0;
      const active = buttons.find(button => button.classList.contains('active'));
      indicator.hidden = !active;
      buttons.forEach(button => describe(button, button === active));
      if (!active || !container.getClientRects().length) return;
      // Local layout offsets ignore entrance transforms on the parent view.
      const x = active.offsetLeft;
      const y = active.offsetTop;
      indicator.style.width = `${active.offsetWidth}px`;
      indicator.style.height = `${active.offsetHeight}px`;
      indicator.style.transform = `translate(${x}px, ${y}px)`;
      container.dataset.indicatorReady = '';
      buttons.forEach(button => describe(button, button === active));
    }

    function schedule() {
      if (!frame) frame = requestAnimationFrame(update);
    }

    const changes = new MutationObserver(schedule);
    buttons.forEach(button => changes.observe(button, {attributes: true, attributeFilter: ['class']}));
    if ('ResizeObserver' in window) {
      const resize = new ResizeObserver(schedule);
      resize.observe(container);
      buttons.forEach(button => resize.observe(button));
    }
    window.addEventListener('resize', schedule, {passive: true});
    document.fonts?.ready.then(schedule);
    update();
  }

  slidingIndicator(document.getElementById('topnav'), '.navbtn', 'nav-active-indicator', (button, selected) => {
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    button.setAttribute('aria-label', button.title);
  });

  document.querySelectorAll('.segmented').forEach(container => {
    container.setAttribute('role', 'group');
    container.setAttribute('aria-label', '任务运行环境');
    slidingIndicator(container, '.tab-btn', 'segment-indicator', (button, selected) => {
      button.setAttribute('aria-pressed', String(selected));
    });
  });


})();
