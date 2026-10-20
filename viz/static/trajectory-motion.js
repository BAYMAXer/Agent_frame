/* Delegation also covers cards inserted by live events and history requests. */
(() => {
  const active = new WeakMap();
  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.target.closest('.wb')) return;
    const summary = event.target.closest('summary');
    const panel = summary?.parentElement;
    if (!panel?.matches('details') || !panel.closest('#local-body, #history-body, #timeline')) return;
    if (event.target.closest('button, a, input, select, textarea')) return;
    event.preventDefault();
    let content = panel.querySelector(':scope > .trajectory-disclosure-body');
    if (!content) {
      content = document.createElement('div');
      content.className = 'trajectory-disclosure-body';
      [...panel.childNodes].filter(node => node !== summary).forEach(node => content.appendChild(node));
      panel.appendChild(content);
    }
    const previous = active.get(panel);
    const start = panel.getBoundingClientRect().height;
    const expanded = previous ? !previous.expanded : !panel.open;
    previous?.animation?.cancel();
    panel.dataset.trajectoryMotion = '';
    summary.setAttribute('aria-expanded', String(expanded));
    content.inert = !expanded;
    const finish = () => {
      panel.open = expanded;
      panel.style.overflow = '';
      active.delete(panel);
    };
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || !panel.animate) { finish(); return; }
    panel.open = true;
    const style = getComputedStyle(panel);
    const end = expanded ? panel.getBoundingClientRect().height : summary.getBoundingClientRect().height +
      parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    panel.style.overflow = 'hidden';
    const animation = panel.animate([{height: start + 'px'}, {height: end + 'px'}], {
      duration: 240, easing: 'cubic-bezier(.2,.7,.2,1)'
    });
    active.set(panel, {animation, expanded});
    animation.onfinish = finish;
  });
})();
