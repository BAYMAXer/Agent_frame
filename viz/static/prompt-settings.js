/* Animate both directions on browsers without native details interpolation. */
(() => {
  const panel = document.getElementById('prompt-settings');
  const summary = panel.querySelector('summary');
  const content = document.getElementById('prompt-settings-body');
  let animation = null;
  let expanded = panel.open;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  function finish() {
    panel.open = expanded;
    panel.style.overflow = '';
    animation = null;
  }
  summary.addEventListener('click', event => {
    event.preventDefault();
    const start = panel.getBoundingClientRect().height;
    if (animation) animation.cancel();
    expanded = !expanded;
    summary.setAttribute('aria-expanded', String(expanded));
    content.inert = !expanded;
    if (reduced.matches || !panel.animate) { finish(); return; }
    panel.open = true;
    const end = expanded ? panel.getBoundingClientRect().height : summary.offsetHeight + 2;
    panel.style.overflow = 'hidden';
    animation = panel.animate([{height: start + 'px'}, {height: end + 'px'}], {
      duration: 260, easing: 'cubic-bezier(.2,.7,.2,1)'
    });
    animation.onfinish = finish;
  });
})();
