"""iLearning 课程自动完成（Python Playwright 版）。

把 skills/ilearning/complete_course.js（Node Playwright）逐行翻译成框架的
Python Playwright：外层控制流（导航/目录/逐章消费/验证）翻译成 async 函数，
内嵌的页面内 JS 片段（CATALOG_JS / INSPECTOR_JS / QUIZ_* / IEXAM_*）保留为字符串，
通过 page.evaluate 注入页面执行。

对外入口：async def complete_course(page) -> dict
"""
from __future__ import annotations

import asyncio
import json
import re

# --------------------------------------------------------------------------- #
# 页面内 JS 片段（对应 complete_course.js 里的模板字符串；用 raw 字符串保留 \\s 等）
# --------------------------------------------------------------------------- #
CATALOG_JS = r"""
(() => {
  const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
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
})()
"""

INSPECTOR_JS = r"""
(() => {
  const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
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
  const quizText = /单选题|多选题|判断题|选择题|提交答案|下一题|答题卡|得分|题目|单选|多选|选项|knowledge check|assessment question|\bquestion\b|answer|option|submit/i.test(body);
  const quizContainer = contentArea.querySelector('[class*="quiz" i], [class*="question" i], [class*="homework" i], [class*="exam" i], [class*="test" i]');
  const quiz = Boolean(quizContainer) || quizText || quizInputs.length > 0;
  const examText = /正式考试|认证考试|final assessment|proctored|闭卷考试|\bexam(?:ination)?\b/i.test(body);
  const exam = examText && !/搜索|search/i.test(body);
  const pageBody = norm(document.body.innerText).slice(0, 20000);
  const login = /sign in|log in|username|password/i.test(pageBody) && Boolean(document.querySelector('input[type="password"]'));
  const captcha = /captcha|verify you are human|recaptcha|验证码/i.test(pageBody) || Boolean(document.querySelector('iframe[src*="captcha" i]'));
  const article = Boolean(contentArea.querySelector('article, [role="article"], [class*="rich-text"], [class*="article"]')) && body.length > 150;
  return { body, signals, hasVideo, hasDoc, quiz, exam, login, captcha, article };
})()
"""

QUIZ_EXTRACT = r"""
(() => {
  const area = document.querySelector('.contentArea') || document.body;
  const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
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
})()
"""

QUIZ_ANSWER = r"""
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
}
"""

IEXAM_EXTRACT = r"""
(() => {
  const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
  const typeText = norm(document.querySelector('.subject-type')?.textContent || '');
  const type = /多选/i.test(typeText) ? 'multiple' : 'single';
  const stem = norm(document.querySelector('.subtitle .main-title .content')?.textContent) ||
    norm(document.querySelector('.subtitle .content')?.textContent) || '';
  const options = [...document.querySelectorAll('.option-list-item .option-list')].map((opt, i) => {
    const order = norm(opt.querySelector('.option-order-str')?.textContent || '');
    const text = norm(opt.querySelector('.option-content .markdown-viewer-program .content, .option-content .content')?.textContent) || '';
    const label = order.replace(/[.\s]/g, '') || String.fromCharCode(65 + i);
    return { label, text };
  });
  return { index: 0, type, stem, options };
})()
"""

IEXAM_ANSWER = r"""
(answers) => {
  const options = [...document.querySelectorAll('.option-list-item .option-list')];
  const clicked = [];
  for (const a of answers || []) {
    for (const choice of a.choices || []) {
      const idx = /^\d+$/.test(String(choice)) ? parseInt(String(choice), 10) : String(choice).toUpperCase().charCodeAt(0) - 65;
      const opt = options[idx];
      if (opt) { opt.click(); clicked.push(idx); }
    }
  }
  return { ok: clicked.length > 0, clicked };
}
"""

# 考试页（examContent）自动答题：循环「选第一个未选选项 → 下一题」，最后交卷。
# 基于真实 DOM：选项 .option-list-item（内部 input[type=radio/checkbox]）、
# 下一题 .subject-btn、交卷 .hand-exams-btn。
EXAM_AUTO_JS = r"""
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
let answered = 0;
let clickedAny = false;
for (let i = 0; i < 80; i++) {
  const items = [...document.querySelectorAll('.option-list-item')];
  let clicked = false;
  for (const it of items) {
    const inp = it.querySelector('input[type="radio"], input[type="checkbox"]');
    if (inp && !inp.checked && !inp.disabled) {
      const t = it.querySelector('label') || it.querySelector('.option-list') || inp;
      t.click();
      clicked = true;
      clickedAny = true;
      break;
    }
  }
  await sleep(250);
  const btns = [...document.querySelectorAll('.subject-btn')];
  const next = btns.find((b) => norm(b.textContent) === '下一题');
  if (!next) break;
  next.click();
  answered++;
  await sleep(350);
}
const submit = document.querySelector('.hand-exams-btn');
let submitted = false;
if (submit) { submit.click(); submitted = true; await sleep(500); }
return { ok: true, answered, clickedAny, submitted };
"""

# --------------------------------------------------------------------------- #
# Qwen 答题服务（对应 qwen.py）
# --------------------------------------------------------------------------- #
QWEN = {
    "base_url": "http://10.90.50.146:18080/v1",
    "api_key": "",
    "model": "qwen3.6-27b",
}

MAX_TEXT_SCROLLS = 30
MAX_DOC_PAGES = 200
VIDEO_TIMEOUT_MS = 4 * 60 * 60 * 1000
PLAYBACK_RATE = 16
SEEK_MARGIN = 8
MAX_PASSES = 500

_VIDEO_SEEK_FN = """(el, o) => {
  const x = el;
  if (o.margin > 0 && Number.isFinite(x.duration) && x.duration > o.margin + 5) {
    x.currentTime = Math.max(0, x.duration - o.margin);
  }
  x.playbackRate = o.rate;
  x.play();
}"""
_VIDEO_STATE_FN = """(el, r) => {
  const x = el;
  if (x.playbackRate !== r) x.playbackRate = r;
  return { ended: x.ended, cur: x.currentTime, dur: x.duration, paused: x.paused, rate: x.playbackRate };
}"""
_VIDEO_REPLAY_FN = """(el, r) => { const x = el; x.playbackRate = r; x.play().catch(() => {}); }"""
_DOC_DIMS_FN = """(e) => ({ sh: e.scrollHeight, ch: e.clientHeight })"""
_DOC_SCROLL_FN = """(e, t) => { e.scrollTop = t; }"""
_DOC_BOTTOM_FN = """(e) => { e.scrollTop = e.scrollHeight; }"""


def _key(x):
    return f"{x.get('index')}::{x.get('title')}"


async def _qwen_answer(questions):
    """批量调用内网 Qwen 服务答题，返回 answers 列表。"""
    import httpx
    answers = []
    BATCH = 6
    for i in range(0, len(questions), BATCH):
        batch = questions[i:i + BATCH]
        ok = False
        for attempt in range(3):
            if ok:
                break
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        f"{QWEN['base_url']}/chat/completions",
                        headers={"Authorization": f"Bearer {QWEN['api_key']}", "Content-Type": "application/json"},
                        json={"model": QWEN["model"], "messages": [
                            {"role": "system",
                             "content": "你是课程测验答题助手。对每一道题给出最可能正确的答案。单选题 choices 只含一个选项字母；多选题 choices 含多个选项字母。必须只输出一个 JSON 对象，形如 {\"answers\":[{\"index\":0,\"choices\":[\"A\"]},{\"index\":1,\"choices\":[\"B\",\"D\"]}]}，不要输出任何其他文字或解释。"},
                            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
                        ], "temperature": 0})
                data = resp.json()
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                idx = content.rfind("</think>")
                if idx >= 0:
                    content = content[idx + len("</think>"):].strip()
                m = re.search(r"\{[\s\S]*\}", content)
                if m:
                    obj = json.loads(m.group(0))
                    if isinstance(obj.get("answers"), list):
                        answers.extend(obj["answers"])
                        ok = True
            except Exception:
                await asyncio.sleep(2.0 * (attempt + 1))
        if not ok:
            raise RuntimeError("Qwen answer service unavailable")
    return answers


async def complete_course(page) -> dict:
    """在指定课程页上自动完成整门课，返回结果对象。"""
    log = []

    def say(m):
        log.append(m)

    course_url = page.url

    async def wait_for_catalog(timeout_ms=30000):
        import time as _t
        deadline = _t.monotonic() * 1000 + timeout_ms
        while _t.monotonic() * 1000 < deadline:
            try:
                c = await page.locator('.catalog-tree-list .tree-node, [role="treeitem"], [class*="chapter-item"]').count()
                if c > 0:
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(500)
        return False

    async def extract_catalog():
        return await page.evaluate(CATALOG_JS)

    async def inspect_page():
        raw = await page.evaluate(INSPECTOR_JS)
        stop_reason = None
        if raw.get("login"):
            stop_reason = "login"
        elif raw.get("captcha"):
            stop_reason = "captcha"
        elif raw.get("exam"):
            stop_reason = "exam"
        elif raw.get("quiz"):
            stop_reason = "quiz"
        kind = "unknown"
        if raw.get("exam"):
            kind = "exam"
        elif raw.get("quiz"):
            kind = "quiz"
        elif raw.get("hasVideo"):
            kind = "video"
        elif raw.get("hasDoc"):
            kind = "document"
        elif raw.get("article"):
            kind = "text"
        if kind == "unknown" and not stop_reason:
            stop_reason = "unknown"
        DONE = re.compile(r"(?:^|[\s_-])(completed?|done|passed|success|finished|viewed)(?:$|[\s_-])", re.I)
        CHECK = re.compile(r"(?:checked?|tick|done|completed?|success)", re.I)
        GREEN = re.compile(r"(?:rgb\(\s*(?:0|[1-9]\d?|1[01]\d|12[0-8])\s*,\s*(?:9\d|1\d\d|2[0-5]\d)\s*,\s*(?:0|[1-9]\d?|1\d\d)\s*\)|green)", re.I)
        explicit = semantic_check = green_check = False
        for sig in raw.get("signals") or []:
            value = (sig.get("value") or "").strip()
            src = sig.get("source") or ""
            if sig.get("explicit") and DONE.search(value):
                explicit = True
            if src in ("svg", "aria", "dom") and CHECK.search(value):
                semantic_check = True
            if src == "style" and GREEN.search(value) and CHECK.search(value):
                green_check = True
        complete = explicit or green_check or (semantic_check and any((s or {}).get("explicit") for s in raw.get("signals") or []))
        return {"kind": kind, "stopReason": stop_reason, "completion": {"complete": complete}}

    async def play_one_video(v, rate, margin):
        await v.scroll_into_view_if_needed()
        try:
            await v.evaluate(_VIDEO_SEEK_FN, {"rate": rate, "margin": margin})
        except Exception:
            pass
        import time as _t
        deadline = _t.monotonic() * 1000 + VIDEO_TIMEOUT_MS
        while _t.monotonic() * 1000 < deadline:
            state = None
            try:
                state = await v.evaluate(_VIDEO_STATE_FN, rate)
            except Exception:
                pass
            if not state:
                await page.wait_for_timeout(2000)
                continue
            dur = state.get("dur") or 0
            cur = state.get("cur") or 0
            if state.get("ended"):
                return
            if dur > 0 and state.get("paused") and cur >= dur - 1.5:
                return
            if state.get("paused") and cur < dur - 1.5:
                try:
                    await v.evaluate(_VIDEO_REPLAY_FN, rate)
                except Exception:
                    pass
            await page.wait_for_timeout(3000)
        raise RuntimeError("Video playback timed out")

    async def consume_video(rate, margin):
        videos = page.locator("video")
        count = await videos.count()
        if count == 0:
            raise RuntimeError("No video found")
        for i in range(count):
            await play_one_video(videos.nth(i), rate, margin)

    async def consume_document():
        scrollers = page.locator(".document-content")
        if await scrollers.count() > 0:
            n = await scrollers.count()
            for i in range(n):
                el = scrollers.nth(i)
                dims = await el.evaluate(_DOC_DIMS_FN)
                sh = dims.get("sh") or 0
                ch = max(1, dims.get("ch") or 600)
                top = 0
                steps = 0
                while top < sh and steps < MAX_DOC_PAGES:
                    await el.evaluate(_DOC_SCROLL_FN, top)
                    await page.wait_for_timeout(250)
                    top += ch
                    steps += 1
                await el.evaluate(_DOC_BOTTOM_FN)
                await page.wait_for_timeout(800)
            return
        pager = page.locator(".ep-icon-caret-right")
        if await pager.count() > 0:
            for _ in range(MAX_DOC_PAGES):
                nxt = pager.first
                cls = (await nxt.get_attribute("class")) or ""
                if "disabled" in cls:
                    break
                try:
                    await nxt.click(force=True)
                except Exception:
                    pass
                await page.wait_for_timeout(800)
            return
        for _ in range(MAX_DOC_PAGES):
            nxt = page.locator('button, a, [class*="next"], [aria-label*="next" i], [aria-label*="下一页"]') \
                .filter(has_text=re.compile(r"next|下一页|下一张|继续", re.I)).first
            if await nxt.count() == 0:
                return
            if not await nxt.is_visible():
                return
            if await nxt.is_disabled():
                return
            await nxt.click()
            await page.wait_for_timeout(700)

    async def consume_text():
        for _ in range(MAX_TEXT_SCROLLS):
            end = await page.evaluate("() => innerHeight + scrollY >= document.documentElement.scrollHeight - 4")
            if end:
                return
            vh = await page.evaluate("() => innerHeight")
            await page.mouse.wheel(0, max(400, int(vh * 0.8)))
            await page.wait_for_timeout(500)
        raise RuntimeError("Text scroll bound reached")

    async def consume_quiz():
        questions = await page.evaluate(QUIZ_EXTRACT)
        if not isinstance(questions, list) or not questions:
            raise RuntimeError("No quiz questions found")
        answers = await _qwen_answer(questions)
        say(f"[quiz] {len(questions)} 题，已生成 {len(answers)} 条答案")
        await page.evaluate(f"({QUIZ_ANSWER})({json.dumps(answers, ensure_ascii=False)})")
        submit = page.locator('button[class*="submit"], [class*="submit-btn"]').first
        if await submit.count() > 0:
            await submit.scroll_into_view_if_needed()
            try:
                await submit.click()
            except Exception:
                pass
            await page.wait_for_timeout(2500)

    async def consume_survey():
        star_groups = page.locator("ul.kltCourse-rate")
        star_count = await star_groups.count()
        for i in range(star_count):
            fifth = star_groups.nth(i).locator('div[role="radio"]').nth(4)
            if await fifth.count() > 0:
                await fifth.scroll_into_view_if_needed()
                try:
                    await fifth.click()
                except Exception:
                    pass
                await page.wait_for_timeout(150)
        radio_names = await page.evaluate("() => [...new Set(Array.from(document.querySelectorAll('input[type=\"radio\"]')).map((r) => r.name))]")
        for name in radio_names:
            if not name:
                continue
            target = page.locator(f'input[type="radio"][name="{name}"][value="3"]').first
            if await target.count() == 0:
                continue
            label = target.locator("xpath=ancestor::label[1]")
            if await label.count() > 0:
                await label.scroll_into_view_if_needed()
                try:
                    await label.click()
                except Exception:
                    pass
                await page.wait_for_timeout(120)
        await page.evaluate("() => { for (const ta of document.querySelectorAll('textarea')) { if (!ta.value.trim()) { ta.value = '很好'; ta.dispatchEvent(new Event('input', { bubbles: true })); ta.dispatchEvent(new Event('change', { bubbles: true })); } } }")
        await page.wait_for_timeout(500)
        submit = page.locator('button[class*="submit-btn"], button[class*="bottom-btn"], button[class*="kltCourse-btn"]').first
        if await submit.count() > 0:
            disabled = True
            try:
                disabled = await submit.is_disabled()
            except Exception:
                pass
            if not disabled:
                await submit.scroll_into_view_if_needed()
                try:
                    await submit.click(force=True)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

    async def consume_exam():
        start_btn = page.locator(".btn.primary").filter(has_text=re.compile(r"开始考试|继续考试|进入考试|重新考试"))
        if not await start_btn.is_visible():
            raise RuntimeError("No exam start button")
        async with page.expect_popup() as popup_info:
            await start_btn.click()
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(4000)
        answered = 0
        for _ in range(300):
            q = await popup.evaluate(IEXAM_EXTRACT)
            if not q or not q.get("stem") or not (q.get("options") or []):
                break
            answers = await _qwen_answer([q])
            if not answers:
                raise RuntimeError("No answer")
            res = await popup.evaluate(f"({IEXAM_ANSWER})({json.dumps(answers, ensure_ascii=False)})")
            if not res or not res.get("ok"):
                raise RuntimeError("Answer not checked")
            answered += 1
            nxt = popup.locator(".vnext").first
            cls = (await nxt.get_attribute("class")) or ""
            if "disabled" in cls:
                break
            try:
                await nxt.click()
            except Exception:
                pass
            await page.wait_for_timeout(900)
        if answered == 0:
            raise RuntimeError("No exam questions answered")
        try:
            await popup.locator(".hand-exams-btn").first.click()
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        try:
            await popup.locator('button, [class*="btn"], [class*="primary"]').filter(has_text=re.compile(r"确定|确认|提交|交卷")).last.click()
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        try:
            await popup.close()
        except Exception:
            pass

    async def open_item(item):
        if item.get("href"):
            await page.goto(item["href"], wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            return
        tree_content = page.locator(".tree-node-content").nth(item.get("index", 0))
        if await tree_content.count() > 0 and await tree_content.is_visible():
            await tree_content.scroll_into_view_if_needed()
            await tree_content.click()
            await page.wait_for_timeout(2000)
            return
        if item.get("parentIndex", -1) >= 0:
            parent = page.locator(".tree-node-content").nth(item["parentIndex"])
            if await parent.count() > 0 and await parent.is_visible():
                await parent.scroll_into_view_if_needed()
                await parent.click()
                await page.wait_for_timeout(2000)
                return
        loc = page.locator('.tree-node-content, li, a, [role="treeitem"], [class*="chapter"], [class*="lesson"]') \
            .filter(has_text=re.compile(re.escape(item.get("title", "")))).first
        await loc.scroll_into_view_if_needed()
        await loc.click()
        await page.wait_for_timeout(1500)

    async def open_and_consume(item, margin):
        catalog = await extract_catalog()
        cur = next((x for x in catalog["items"] if _key(x) == _key(item)), item)
        ancestors = []
        idx = cur.get("parentIndex", -1)
        while idx >= 0:
            parent = next((x for x in catalog["items"] if x.get("index") == idx), None)
            if not parent:
                break
            ancestors.insert(0, parent)
            idx = parent.get("parentIndex", -1)
        for p in ancestors:
            if not p.get("expanded"):
                content = page.locator(".tree-node-content").nth(p.get("index", 0))
                try:
                    if await content.count() > 0 and await content.is_visible():
                        await content.click()
                        await page.wait_for_timeout(800)
                except Exception:
                    pass
        await open_item(cur)
        inspection = await inspect_page()
        sr = inspection.get("stopReason")
        if sr and sr not in ("quiz", "exam"):
            return {"stopReason": sr}
        if inspection["completion"]["complete"]:
            return {}
        kind = inspection["kind"]
        if kind == "video":
            await consume_video(PLAYBACK_RATE, margin)
        elif kind == "document":
            await consume_document()
        elif kind == "text":
            await consume_text()
        elif kind in ("quiz", "exam"):
            has_start = await page.locator(".btn.primary").filter(has_text=re.compile(r"开始考试|继续考试|进入考试")).is_visible()
            if has_start:
                await consume_exam()
            else:
                has_choice = await page.locator(".choice-type").count() > 0
                if has_choice:
                    await consume_quiz()
                else:
                    await consume_survey()
        else:
            raise RuntimeError("Unsupported content kind: " + kind)
        return {}

    async def verify_chapter(item_id):
        await page.goto(course_url, wait_until="domcontentloaded")
        await wait_for_catalog()
        after = await extract_catalog()
        return next((x for x in after["items"] if _key(x) == item_id), None)

    # ---- 主流程 ----
    await page.goto(course_url, wait_until="domcontentloaded")
    if not await wait_for_catalog():
        raise RuntimeError("目录未加载，可能未登录或页面异常")
    initial = await extract_catalog()
    total_leaves = len([x for x in initial["items"] if not x.get("hasChildren")])
    processed = set()
    failed = set()
    stopped_reason = None
    say(f"[start] 共 {total_leaves} 章")

    for _pass in range(MAX_PASSES):
        catalog = await extract_catalog()
        leaves = [x for x in catalog["items"] if not x.get("hasChildren")]
        target = next((x for x in leaves if not x.get("completed") and _key(x) not in processed and _key(x) not in failed), None)
        if not target:
            break
        item_id = _key(target)
        try:
            first = await open_and_consume(target, SEEK_MARGIN)
            if first.get("stopReason"):
                if first["stopReason"] in ("login", "captcha"):
                    stopped_reason = first["stopReason"]
                    break
                failed.add(item_id)
                say(f"[skip] {target['title']} — {first['stopReason']}")
                await page.goto(course_url, wait_until="domcontentloaded")
                await wait_for_catalog()
                continue
            after = await verify_chapter(item_id)
            if not after or (not after.get("completed") and target.get("kind") == "video" and SEEK_MARGIN > 0):
                say(f"[retry-full] {target['title']} — 快进未生效，全速重播")
                await open_and_consume(target, 0)
                after = await verify_chapter(item_id)
            if after and after.get("completed"):
                processed.add(item_id)
                say(f"[ok] {target['title']}")
            else:
                failed.add(item_id)
                say(f"[unverified] {target['title']} — 刷新后无完成标记")
        except Exception as e:
            failed.add(item_id)
            say(f"[error] {target['title']}: {e}")
            try:
                await page.goto(course_url, wait_until="domcontentloaded")
                await wait_for_catalog()
            except Exception:
                pass

    try:
        await page.goto(course_url, wait_until="domcontentloaded")
        await wait_for_catalog()
    except Exception:
        pass

    final = await extract_catalog()
    final_leaves = [x for x in final["items"] if not x.get("hasChildren")]
    completed_after = len([x for x in final_leaves if x.get("completed")])
    remaining = [x.get("title") for x in final_leaves if not x.get("completed")]
    say(f"[summary] 完成 {completed_after}/{total_leaves} 章" + (f"（停止：{stopped_reason}）" if stopped_reason else ""))
    if remaining:
        say(f"[remaining] 仍未通过: {' | '.join(remaining)}")

    return {
        "ok": completed_after == total_leaves,
        "completedAfter": completed_after,
        "total": total_leaves,
        "remaining": remaining,
        "processed": [x.split("::")[1] if "::" in x else x for x in sorted(processed)],
        "failed": [x.split("::")[1] if "::" in x else x for x in sorted(failed)],
        "stoppedReason": stopped_reason,
        "log": log,
    }
