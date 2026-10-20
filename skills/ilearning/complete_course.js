async (page) => {
  const courseUrl = page.url();
  const log = [];
  const say = (m) => { log.push(m); console.log(m); };

  // ---------------- 页面内 JS 片段（对应 ilearning/scripts/*.js） ----------------

  const CATALOG_JS = `
(() => {
  const norm = (s) => (s ?? '').replace(/\\s+/g, ' ').trim();
  const treeNodes = [...document.querySelectorAll('.catalog-tree-list .tree-node')];
  if (treeNodes.length > 0) {
    const base = treeNodes.map((node, index) => {
      const content = node.querySelector(':scope > .tree-node-content') || node.querySelector('.tree-node-content');
      const textEl = content ? (content.querySelector('.text') || content.querySelector('p')) : null;
      const title = norm(textEl?.textContent || content?.textContent || '').slice(0, 200);
      const completed = Boolean(content && content.querySelector('.anticon-check-circle, [aria-label="check-circle"], [aria-label*="check" i]'));
      const use = content?.querySelector('.header-icon use') || content?.querySelector('use');
      const iconHref = norm(use?.getAttribute('href') || use?.getAttribute('xlink:href'));
      let kind = 'unknown';
      if (/video|play|film|tv|media/i.test(iconHref)) kind = 'video';
      else if (/ppt|slide|doc|pdf|file|book|read|article|word/i.test(iconHref)) kind = 'document';
      else if (/quiz|exam|test|question|homework|survey/i.test(iconHref)) kind = 'quiz';
      if (kind === 'unknown') {
        const t = title;
        if (/考试|测验|测试|练习|考核|exam|quiz|test/i.test(t)) kind = 'quiz';
        else if (/课件|文档|讲义|资料|pdf|ppt|材料|课程材料/i.test(t)) kind = 'document';
        else if (/视频|video/i.test(t)) kind = 'video';
      }
      let level = 1;
      let p = node.parentElement;
      while (p && p !== document.body) {
        if (p.classList && p.classList.contains('tree-node')) level++;
        p = p.parentElement;
      }
      const childrenEl = node.querySelector(':scope > .tree-node-children');
      const hasChildren = Boolean(childrenEl && childrenEl.querySelector('.tree-node'));
      const expanded = Boolean(childrenEl && childrenEl.classList.contains('is-expanded'));
      return { index, title, href: undefined, level, completed, hasChildren, expanded, kind };
    });
    const items = base.map((b, index) => {
      let parentIndex = -1;
      for (let i = index - 1; i >= 0; i--) {
        if (base[i].hasChildren && base[i].level < b.level) { parentIndex = i; break; }
      }
      return {
        index: b.index, title: b.title, href: b.href, level: b.level,
        completed: b.completed, confidence: b.completed ? 'high' : 'none',
        evidence: b.completed ? ['anticon-check-circle green check'] : [],
        hasChildren: b.hasChildren, expanded: b.expanded, parentIndex, kind: b.kind
      };
    });
    return { url: location.href, rootDescription: 'iLearning .catalog-tree-list', total: items.length,
             completedCount: items.filter((x) => x.completed).length, items };
  }
  return { url: location.href, total: 0, completedCount: 0, items: [] };
})()`;

  const INSPECTOR_JS = `
(() => {
  const norm = (s) => (s ?? '').replace(/\\s+/g, ' ').trim();
  const contentArea = document.querySelector('.contentArea') || document.querySelector('.learn-content-main') ||
    document.querySelector('.center-content') || document.body;
  const body = norm(contentArea.innerText).slice(0, 20000);
  const signals = [];
  const selectors = ['[aria-label*="complete" i]', '[aria-label*="done" i]', '[aria-label*="check" i]',
    '[data-status]', '[data-completed]', '[class*="complete" i]', '[class*="success" i]',
    'svg[aria-label]', 'svg title', '[role="img"]'];
  for (const el of Array.from(contentArea.querySelectorAll(selectors.join(','))).slice(0, 100)) {
    const aria = norm(el.getAttribute('aria-label'));
    const state = norm(el.getAttribute('data-status') ?? el.getAttribute('data-completed'));
    const cls = norm(el.getAttribute('class'));
    const text = norm(el.textContent).slice(0, 200);
    const value = [aria, state, cls, text].filter(Boolean).join(' | ');
    const source = el.closest('svg') ? 'svg' : aria ? 'aria' : 'dom';
    const explicit = /complete(?:d)?|done|passed|success|finished|viewed/i.test(aria + ' ' + state + ' ' + cls);
    if (value) signals.push({ source, value, explicit });
    const style = getComputedStyle(el);
    if (/check|tick|done|complete|success/i.test(value)) {
      signals.push({ source: 'style', value: value + ' color:' + style.color + ' background:' + style.backgroundColor, explicit: false });
    }
  }
  const hasVideo = Boolean(contentArea.querySelector('video'));
  const hasDoc = Boolean(contentArea.querySelector('iframe[src*="pdf" i], embed[type="application/pdf"], [class*="ppt" i], [class*="slide" i], [class*="pdf" i], [class*="document" i], [class*="office"]'));
  const quizInputs = [...contentArea.querySelectorAll('input[type="radio"], input[type="checkbox"]')]
    .filter((el) => !el.closest('[class*="vjs-"], [class*="player"], [class*="setting"], [class*="track-setting"], video, [role="switch"], [class*="switch"]'));
  const quizText = /单选题|多选题|判断题|选择题|提交答案|下一题|答题卡|得分|题目|单选|多选|选项|knowledge check|assessment question|\\bquestion\\b|answer|option|submit/i.test(body);
  const quizContainer = contentArea.querySelector('[class*="quiz" i], [class*="question" i], [class*="homework" i], [class*="exam" i], [class*="test" i]');
  const quiz = Boolean(quizContainer) || quizText || quizInputs.length > 0;
  const examText = /正式考试|认证考试|final assessment|proctored|闭卷考试|\\bexam(?:ination)?\\b/i.test(body);
  const exam = examText && !/搜索|search/i.test(body);
  const pageBody = norm(document.body.innerText).slice(0, 20000);
  const login = /sign in|log in|username|password/i.test(pageBody) && Boolean(document.querySelector('input[type="password"]'));
  const captcha = /captcha|verify you are human|recaptcha|验证码/i.test(pageBody) || Boolean(document.querySelector('iframe[src*="captcha" i]'));
  const article = Boolean(contentArea.querySelector('article, [role="article"], [class*="rich-text"], [class*="article"]')) && body.length > 150;
  return { body, signals, hasVideo, hasDoc, quiz, exam, login, captcha, article };
})()`;

  const QUIZ_EXTRACT = `
(() => {
  const area = document.querySelector('.contentArea') || document.body;
  const norm = (s) => (s ?? '').replace(/\\s+/g, ' ').trim();
  const containers = [...area.querySelectorAll('.choice-type')].map((el) => el.parentElement);
  return containers.map((q, i) => {
    const typeEl = q.querySelector('.subject-type');
    const typeText = norm(typeEl ? typeEl.textContent : '');
    const type = typeText.includes('多选') ? 'multiple' : 'single';
    const stemEl = q.querySelector('.subject-title');
    const stem = norm(stemEl ? stemEl.textContent : '');
    const options = [...q.querySelectorAll('.choice-option')].map((opt, j) => {
      const labelEl = opt.querySelector('.option-label');
      return { label: String.fromCharCode(65 + j), text: norm(labelEl ? labelEl.textContent : '') };
    });
    return { index: i, type, stem, options };
  }).filter((q) => q.stem && q.options.length > 0);
})()`;

  const QUIZ_ANSWER = `
(answers) => {
  const containers = [...document.querySelectorAll('.choice-type')].map((el) => el.parentElement);
  const results = [];
  for (const ans of answers || []) {
    const q = containers[ans.index];
    if (!q) { results.push({ index: ans.index, ok: false, reason: 'missing' }); continue; }
    const opts = [...q.querySelectorAll('.choice-option')];
    let clicked = 0;
    for (const choice of ans.choices || []) {
      const idx = choice.charCodeAt(0) - 65;
      const opt = opts[idx];
      if (!opt) continue;
      const label = opt.querySelector('.option-label');
      const input = opt.querySelector('input[type="radio"], input[type="checkbox"]');
      const target = label || input;
      if (target) { target.click(); clicked++; }
    }
    results.push({ index: ans.index, ok: clicked > 0, clicked });
  }
  return results;
}`;

  const IEXAM_EXTRACT = `
(() => {
  const norm = (s) => (s ?? '').replace(/\\s+/g, ' ').trim();
  const typeText = norm(document.querySelector('.subject-type')?.textContent || '');
  const type = /多选/i.test(typeText) ? 'multiple' : 'single';
  const stem = norm(document.querySelector('.subtitle .main-title .content')?.textContent) ||
    norm(document.querySelector('.subtitle .content')?.textContent) || '';
  const options = [...document.querySelectorAll('.option-list-item .option-list')].map((opt, i) => {
    const order = norm(opt.querySelector('.option-order-str')?.textContent || '');
    const text = norm(opt.querySelector('.option-content .markdown-viewer-program .content, .option-content .content')?.textContent) || '';
    const label = order.replace(/[.\\s]/g, '') || String.fromCharCode(65 + i);
    return { label, text };
  });
  return { index: 0, type, stem, options };
})()`;

  const IEXAM_ANSWER = `
(answers) => {
  const options = [...document.querySelectorAll('.option-list-item .option-list')];
  const clicked = [];
  for (const a of answers || []) {
    for (const choice of a.choices || []) {
      const idx = /^\\d+$/.test(String(choice)) ? parseInt(String(choice), 10) : String(choice).toUpperCase().charCodeAt(0) - 65;
      const opt = options[idx];
      if (opt) { opt.click(); clicked.push(idx); }
    }
  }
  return { ok: clicked.length > 0, clicked };
}`;

  // ---------------- Qwen 答题服务（对应 qwen.py） ----------------
  const QWEN = {
    baseUrl: 'http://10.90.50.146:18080/v1',
    apiKey: '',
    model: 'qwen3.6-27b',
  };

  // ---------------- 运行参数（对应 types.py RunOptions） ----------------
  const MAX_TEXT_SCROLLS = 30;
  const MAX_DOC_PAGES = 200;
  const VIDEO_TIMEOUT_MS = 4 * 60 * 60 * 1000;
  const PLAYBACK_RATE = 16;
  const SEEK_MARGIN = 8;
  const MAX_PASSES = 500;

  const sleep = (ms) => page.waitForTimeout(ms);
  const key = (x) => `${x.index}::${x.title}`;

  // ---------------- 目录（对应 catalog.py） ----------------
  async function waitForCatalog(timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const c = await page.locator('.catalog-tree-list .tree-node, [role="treeitem"], [class*="chapter-item"]').count();
      if (c > 0) return true;
      await sleep(500);
    }
    return false;
  }
  async function extractCatalog() { return await page.evaluate(CATALOG_JS); }

  // ---------------- 内容判定（对应 inspector.py + completion.py） ----------------
  async function inspectPage() {
    const raw = await page.evaluate(INSPECTOR_JS);
    let stopReason = null;
    if (raw.login) stopReason = 'login';
    else if (raw.captcha) stopReason = 'captcha';
    else if (raw.exam) stopReason = 'exam';
    else if (raw.quiz) stopReason = 'quiz';
    let kind = 'unknown';
    if (raw.exam) kind = 'exam';
    else if (raw.quiz) kind = 'quiz';
    else if (raw.hasVideo) kind = 'video';
    else if (raw.hasDoc) kind = 'document';
    else if (raw.article) kind = 'text';
    if (kind === 'unknown' && !stopReason) stopReason = 'unknown';

    const DONE = /(?:^|[\s_-])(completed?|done|passed|success|finished|viewed)(?:$|[\s_-])/i;
    const CHECK = /(?:checked?|tick|done|completed?|success)/i;
    const GREEN = /(?:rgb\(\s*(?:0|[1-9]\d?|1[01]\d|12[0-8])\s*,\s*(?:9\d|1\d\d|2[0-5]\d)\s*,\s*(?:0|[1-9]\d?|1\d\d)\s*\)|green)/i;
    let explicit = false, semanticCheck = false, greenCheck = false;
    for (const sig of raw.signals || []) {
      const value = (sig.value || '').trim();
      const src = sig.source || '';
      if (sig.explicit && DONE.test(value)) explicit = true;
      if (['svg', 'aria', 'dom'].includes(src) && CHECK.test(value)) semanticCheck = true;
      if (src === 'style' && GREEN.test(value) && CHECK.test(value)) greenCheck = true;
    }
    const complete = explicit || greenCheck || (semanticCheck && (raw.signals || []).some((s) => s.explicit));
    return { kind, stopReason, completion: { complete } };
  }

  // ---------------- 内容消费（对应 consume.py） ----------------
  async function playOneVideo(v, rate, margin) {
    await v.scrollIntoViewIfNeeded();
    try {
      await v.evaluate((el, o) => {
        const x = el;
        if (o.margin > 0 && Number.isFinite(x.duration) && x.duration > o.margin + 5) {
          x.currentTime = Math.max(0, x.duration - o.margin);
        }
        x.playbackRate = o.rate;
        x.play();
      }, { rate, margin });
    } catch (e) {}
    const deadline = Date.now() + VIDEO_TIMEOUT_MS;
    while (Date.now() < deadline) {
      let state = null;
      try {
        state = await v.evaluate((el, r) => {
          const x = el;
          if (x.playbackRate !== r) x.playbackRate = r;
          return { ended: x.ended, cur: x.currentTime, dur: x.duration, paused: x.paused, rate: x.playbackRate };
        }, rate);
      } catch (e) {}
      if (!state) { await sleep(2000); continue; }
      const dur = state.dur || 0, cur = state.cur || 0;
      if (state.ended) return;
      if (dur > 0 && state.paused && cur >= dur - 1.5) return;
      if (state.paused && cur < dur - 1.5) {
        try { await v.evaluate((el, r) => { const x = el; x.playbackRate = r; x.play().catch(() => {}); }, rate); } catch (e) {}
      }
      await sleep(3000);
    }
    throw new Error('Video playback timed out');
  }

  async function consumeVideo(rate, margin) {
    const videos = page.locator('video');
    const count = await videos.count();
    if (count === 0) throw new Error('No video found');
    for (let i = 0; i < count; i++) await playOneVideo(videos.nth(i), rate, margin);
  }

  async function consumeDocument() {
    const scrollers = page.locator('.document-content');
    if ((await scrollers.count()) > 0) {
      const n = await scrollers.count();
      for (let i = 0; i < n; i++) {
        const el = scrollers.nth(i);
        const dims = await el.evaluate((e) => ({ sh: e.scrollHeight, ch: e.clientHeight }));
        const sh = dims.sh || 0, ch = Math.max(1, dims.ch || 600);
        for (let top = 0, steps = 0; top < sh && steps < MAX_DOC_PAGES; top += ch, steps++) {
          await el.evaluate((e, t) => { e.scrollTop = t; }, top);
          await sleep(250);
        }
        await el.evaluate((e) => { e.scrollTop = e.scrollHeight; });
        await sleep(800);
      }
      return;
    }
    const pager = page.locator('.ep-icon-caret-right');
    if ((await pager.count()) > 0) {
      for (let i = 0; i < MAX_DOC_PAGES; i++) {
        const nxt = pager.first;
        const cls = (await nxt.getAttribute('class')) || '';
        if (cls.includes('disabled')) break;
        try { await nxt.click({ force: true }); } catch (e) {}
        await sleep(800);
      }
      return;
    }
    for (let i = 0; i < MAX_DOC_PAGES; i++) {
      const nxt = page.locator('button, a, [class*="next"], [aria-label*="next" i], [aria-label*="下一页"]')
        .filter({ hasText: /next|下一页|下一张|继续/i }).first;
      if ((await nxt.count()) === 0) return;
      if (!(await nxt.isVisible())) return;
      if (await nxt.isDisabled()) return;
      await nxt.click();
      await sleep(700);
    }
  }

  async function consumeText() {
    for (let i = 0; i < MAX_TEXT_SCROLLS; i++) {
      const end = await page.evaluate(() => innerHeight + scrollY >= document.documentElement.scrollHeight - 4);
      if (end) return;
      const vh = await page.evaluate(() => innerHeight);
      await page.mouse.wheel(0, Math.max(400, Math.floor(vh * 0.8)));
      await sleep(500);
    }
    throw new Error('Text scroll bound reached');
  }

  // ---------------- Qwen 批量答题（对应 qwen.py） ----------------
  async function answerQuestions(questions) {
    const answers = [];
    const BATCH = 6;
    for (let i = 0; i < questions.length; i += BATCH) {
      const batch = questions.slice(i, i + BATCH);
      let ok = false;
      for (let attempt = 0; attempt < 3 && !ok; attempt++) {
        try {
          const resp = await fetch(`${QWEN.baseUrl}/chat/completions`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${QWEN.apiKey}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
              model: QWEN.model,
              messages: [
                { role: 'system', content: '你是课程测验答题助手。对每一道题给出最可能正确的答案。单选题 choices 只含一个选项字母；多选题 choices 含多个选项字母。必须只输出一个 JSON 对象，形如 {"answers":[{"index":0,"choices":["A"]},{"index":1,"choices":["B","D"]}]}，不要输出任何其他文字或解释。' },
                { role: 'user', content: JSON.stringify(batch) },
              ],
              temperature: 0,
            }),
          });
          const data = await resp.json();
          let content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
          const idx = content.lastIndexOf('</think>');
          if (idx >= 0) content = content.slice(idx + 8).trim();
          const m = content.match(/\{[\s\S]*\}/);
          if (m) {
            const obj = JSON.parse(m[0]);
            if (Array.isArray(obj.answers)) { answers.push(...obj.answers); ok = true; }
          }
        } catch (e) {
          await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
        }
      }
      if (!ok) throw new Error('Qwen answer service unavailable');
    }
    return answers;
  }

  async function consumeQuiz() {
    const questions = await page.evaluate(QUIZ_EXTRACT);
    if (!Array.isArray(questions) || questions.length === 0) throw new Error('No quiz questions found');
    const answers = await answerQuestions(questions);
    say(`[quiz] ${questions.length} 题，已生成 ${answers.length} 条答案`);
    await page.evaluate(`(${QUIZ_ANSWER})(${JSON.stringify(answers)})`);
    const submit = page.locator('button[class*="submit"], [class*="submit-btn"]').first;
    if ((await submit.count()) > 0) {
      await submit.scrollIntoViewIfNeeded();
      try { await submit.click(); } catch (e) {}
      await sleep(2500);
    }
  }

  async function consumeSurvey() {
    const starGroups = page.locator('ul.kltCourse-rate');
    const starCount = await starGroups.count();
    for (let i = 0; i < starCount; i++) {
      const fifth = starGroups.nth(i).locator('div[role="radio"]').nth(4);
      if ((await fifth.count()) > 0) {
        await fifth.scrollIntoViewIfNeeded();
        try { await fifth.click(); } catch (e) {}
        await sleep(150);
      }
    }
    const radioNames = await page.evaluate('() => [...new Set(Array.from(document.querySelectorAll(\'input[type="radio"]\')).map((r) => r.name))]');
    for (const name of radioNames) {
      if (!name) continue;
      const target = page.locator(`input[type="radio"][name="${name}"][value="3"]`).first;
      if ((await target.count()) === 0) continue;
      const label = target.locator('xpath=ancestor::label[1]');
      if ((await label.count()) > 0) {
        await label.scrollIntoViewIfNeeded();
        try { await label.click(); } catch (e) {}
        await sleep(120);
      }
    }
    await page.evaluate('() => { for (const ta of document.querySelectorAll("textarea")) { if (!ta.value.trim()) { ta.value = "很好"; ta.dispatchEvent(new Event("input", { bubbles: true })); ta.dispatchEvent(new Event("change", { bubbles: true })); } } }');
    await sleep(500);
    const submit = page.locator('button[class*="submit-btn"], button[class*="bottom-btn"], button[class*="kltCourse-btn"]').first;
    if ((await submit.count()) > 0) {
      let disabled = true;
      try { disabled = await submit.isDisabled(); } catch (e) {}
      if (!disabled) {
        await submit.scrollIntoViewIfNeeded();
        try { await submit.click({ force: true }); } catch (e) {}
        await sleep(2500);
      }
    }
  }

  async function consumeExam() {
    const startBtn = page.locator('.btn.primary').filter({ hasText: /开始考试|继续考试|进入考试|重新考试/ });
    if (!(await startBtn.isVisible())) throw new Error('No exam start button');
    const [popup] = await Promise.all([page.waitForEvent('popup'), startBtn.click()]);
    await popup.waitForLoadState('domcontentloaded');
    await sleep(4000);
    let answered = 0;
    for (let i = 0; i < 300; i++) {
      const q = await popup.evaluate(IEXAM_EXTRACT);
      if (!q || !q.stem || (q.options || []).length === 0) break;
      const answers = await answerQuestions([q]);
      if (!answers.length) throw new Error('No answer');
      const res = await popup.evaluate(`(${IEXAM_ANSWER})(${JSON.stringify(answers)})`);
      if (!res || !res.ok) throw new Error('Answer not checked');
      answered++;
      const nxt = popup.locator('.vnext').first;
      const cls = (await nxt.getAttribute('class')) || '';
      if (cls.includes('disabled')) break;
      try { await nxt.click(); } catch (e) {}
      await sleep(900);
    }
    if (answered === 0) throw new Error('No exam questions answered');
    try { await popup.locator('.hand-exams-btn').first.click(); } catch (e) {}
    await sleep(1500);
    try { await popup.locator('button, [class*="btn"], [class*="primary"]').filter({ hasText: /确定|确认|提交|交卷/ }).last.click(); } catch (e) {}
    await sleep(3000);
    try { await popup.close(); } catch (e) {}
  }

  // ---------------- 章节打开与消费（对应 course.py） ----------------
  async function openItem(item) {
    if (item.href) {
      await page.goto(item.href, { waitUntil: 'domcontentloaded' });
      await sleep(1500);
      return;
    }
    const treeContent = page.locator('.tree-node-content').nth(item.index);
    if ((await treeContent.count()) > 0 && (await treeContent.isVisible())) {
      await treeContent.scrollIntoViewIfNeeded();
      await treeContent.click();
      await sleep(2000);
      return;
    }
    if (item.parentIndex >= 0) {
      const parent = page.locator('.tree-node-content').nth(item.parentIndex);
      if ((await parent.count()) > 0 && (await parent.isVisible())) {
        await parent.scrollIntoViewIfNeeded();
        await parent.click();
        await sleep(2000);
        return;
      }
    }
    const loc = page.locator('.tree-node-content, li, a, [role="treeitem"], [class*="chapter"], [class*="lesson"]').filter({ hasText: item.title }).first;
    await loc.scrollIntoViewIfNeeded();
    await loc.click();
    await sleep(1500);
  }

  async function openAndConsume(item, margin) {
    const catalog = await extractCatalog();
    const cur = catalog.items.find((x) => key(x) === key(item)) || item;
    const ancestors = [];
    let idx = cur.parentIndex;
    while (idx >= 0) {
      const parent = catalog.items.find((x) => x.index === idx);
      if (!parent) break;
      ancestors.unshift(parent);
      idx = parent.parentIndex;
    }
    for (const p of ancestors) {
      if (!p.expanded) {
        const content = page.locator('.tree-node-content').nth(p.index);
        try { if ((await content.count()) > 0 && (await content.isVisible())) { await content.click(); await sleep(800); } } catch (e) {}
      }
    }
    await openItem(cur);
    const inspection = await inspectPage();
    const sr = inspection.stopReason;
    if (sr && sr !== 'quiz' && sr !== 'exam') return { stopReason: sr };
    if (inspection.completion.complete) return {};
    switch (inspection.kind) {
      case 'video': await consumeVideo(PLAYBACK_RATE, margin); break;
      case 'document': await consumeDocument(); break;
      case 'text': await consumeText(); break;
      case 'quiz':
      case 'exam': {
        const hasStart = await page.locator('.btn.primary').filter({ hasText: /开始考试|继续考试|进入考试/ }).isVisible();
        if (hasStart) await consumeExam();
        else {
          const hasChoice = (await page.locator('.choice-type').count()) > 0;
          if (hasChoice) await consumeQuiz();
          else await consumeSurvey();
        }
        break;
      }
      default: throw new Error('Unsupported content kind: ' + inspection.kind);
    }
    return {};
  }

  async function verifyChapter(id) {
    await page.goto(courseUrl, { waitUntil: 'domcontentloaded' });
    await waitForCatalog();
    const after = await extractCatalog();
    return after.items.find((x) => key(x) === id);
  }

  // ---------------- 主流程（对应 course.py run_course） ----------------
  await page.goto(courseUrl, { waitUntil: 'domcontentloaded' });
  if (!(await waitForCatalog())) {
    throw new Error('目录未加载，可能未登录或页面异常');
  }
  const initial = await extractCatalog();
  const totalLeaves = initial.items.filter((x) => !x.hasChildren).length;
  const processed = new Set();
  const failed = new Set();
  let stoppedReason = null;

  say(`[start] 共 ${totalLeaves} 章`);

  for (let pass = 0; pass < MAX_PASSES; pass++) {
    const catalog = await extractCatalog();
    const leaves = catalog.items.filter((x) => !x.hasChildren);
    const target = leaves.find((x) => !x.completed && !processed.has(key(x)) && !failed.has(key(x)));
    if (!target) break;
    const id = key(target);
    try {
      const first = await openAndConsume(target, SEEK_MARGIN);
      if (first.stopReason) {
        if (first.stopReason === 'login' || first.stopReason === 'captcha') { stoppedReason = first.stopReason; break; }
        failed.add(id);
        say(`[skip] ${target.title} — ${first.stopReason}`);
        await page.goto(courseUrl, { waitUntil: 'domcontentloaded' });
        await waitForCatalog();
        continue;
      }
      let after = await verifyChapter(id);
      if (!after || (!after.completed && target.kind === 'video' && SEEK_MARGIN > 0)) {
        say(`[retry-full] ${target.title} — 快进未生效，全速重播`);
        await openAndConsume(target, 0);
        after = await verifyChapter(id);
      }
      if (after && after.completed) { processed.add(id); say(`[ok] ${target.title}`); }
      else { failed.add(id); say(`[unverified] ${target.title} — 刷新后无完成标记`); }
    } catch (e) {
      failed.add(id);
      say(`[error] ${target.title}: ${e.message}`);
      try { await page.goto(courseUrl, { waitUntil: 'domcontentloaded' }); await waitForCatalog(); } catch (e2) {}
    }
  }

  // 结束时把页面稳定回课程页，避免留给 agent 一个 about:blank / 空白页
  try { await page.goto(courseUrl, { waitUntil: 'domcontentloaded' }); await waitForCatalog(); } catch (e) {}

  const final = await extractCatalog();
  const finalLeaves = final.items.filter((x) => !x.hasChildren);
  const completedAfter = finalLeaves.filter((x) => x.completed).length;
  const remaining = finalLeaves.filter((x) => !x.completed).map((x) => x.title);
  say(`[summary] 完成 ${completedAfter}/${totalLeaves} 章` + (stoppedReason ? `（停止：${stoppedReason}）` : ''));
  if (remaining.length) say(`[remaining] 仍未通过: ${remaining.join(' | ')}`);

  return {
    ok: completedAfter === totalLeaves,
    completedAfter,
    total: totalLeaves,
    remaining,
    processed: [...processed].map((x) => x.split('::')[1]),
    failed: [...failed].map((x) => x.split('::')[1]),
    stoppedReason,
    log,
  }
}
