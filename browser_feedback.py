"""Small, framework-derived browser facts and conservative pre-dispatch checks."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .paper_resolver import arxiv_id_from_url, query_matches_title
from .browser_results import paper_result_cards
from .scholar_search import english_scholar_url, is_scholar, scholar_title_links
from .scene import browser_context, observation_scene


def exposed_nodes(obs):
    if obs is None or (obs.info or {}).get('active_surface') == 'desktop':
        return []
    exposed = (obs.info or {}).get('exposed_refs')
    return [n for b in browser_context(obs) for n in b.get('nodes', [])
            if exposed is None or n.get('ref') in exposed]


def active_paper(completion, step=None):
    if completion is None:
        return None
    goals = completion.paper_goals()
    ids = set((step or {}).get('check_ids', []))
    ids.update(p.get('check_id') for p in (step or {}).get('verification', [])
               if p.get('kind') in {'download_complete', 'download_valid'})
    selected = [g for g in goals if g['check_id'] in ids]
    goals = selected or goals
    # Do not pick an arbitrary paper for a multi-paper task.
    return goals[0] if len(goals) == 1 and goals[0].get('binding') else None


def same_paper(identifier, expected):
    # Explicit versions remain constraints; an unversioned goal permits versions.
    if re.search(r'v\d+$', expected, re.I):
        return identifier.lower() == expected.lower()
    return re.sub(r'v\d+$', '', identifier, flags=re.I).lower() == expected.lower()


def unbound_paper_candidate_error(action, obs, completion, route=None):
    """Compatibility hook: lexical title differences never reject inspection.

    A user may name an acronym, a translated title or the subject of a paper.
    The model assesses candidate relevance from observed evidence. Navigation
    provenance and the verified-binding download guard remain separate gates.
    """
    return ''


def redundant_sort_selection_error(action, obs, route=None):
    """A selected search field is not a submitted search; avoid control loops."""
    if obs is None or action.action not in {'select', 'click'}:
        return ''
    ref = str((route or {}).get('target_ref') or action.args.get('target_ref') or '')
    option = str(action.args.get('option') or '').strip()
    if not ref and action.action == 'select':
        target = str(action.args.get('target') or '').strip()
        matches = [n for n in exposed_nodes(obs) if n.get('role') == 'combobox'
                   and (target.casefold() == str(n.get('name') or '').casefold()
                        or target.lstrip('#') == n.get('id'))]
        if len(matches) == 1:
            ref = str(matches[0].get('ref') or '')
    if not ref or (action.action == 'select' and not option):
        return ''
    for block in (obs.context or []):
        if block.get('kind') != 'browser_ax':
            continue
        selected = next((node for node in block.get('nodes', [])
                         if str(node.get('ref')) == ref
                         and (node.get('tag') == 'select' or node.get('role') == 'combobox')
                         and (action.action == 'click' or str(node.get('value') or '').strip() == option)), None)
        if selected:
            if action.action == 'click':
                url = urlsplit(observation_url(obs))
                submitted_type = parse_qs(url.query).get('searchtype', [''])[0].casefold()
                if (selected.get('id') == 'searchtype'
                        and str(selected.get('value') or '').strip().casefold() == 'title'
                        and url.path.startswith('/search/') and submitted_type != 'title'):
                    return 'Title 已选中，但搜索尚未提交；不要再点字段下拉框，请点击上方搜索框右侧的 Search 按钮。'
                continue
            if selected.get('id') == 'order':
                return '排序选项已经是“' + option + '”。再次选择不会提交搜索；请点击同一表单的 Go，再观察结果后决定是否更改检索字段。'
            if selected.get('id') == 'searchtype':
                return '搜索字段已经是“' + option + '”。再次选择不会提交搜索；请点击上方搜索框右侧的 Search 按钮。'
            return '该下拉框已经选中“' + option + '”；不要重复选择，请执行后续提交动作。'
    return ''


def observation_url(obs):
    if obs is None:
        return ''
    return (obs.info or {}).get('url') or next((b.get('url', '') for b in obs.context if b.get('kind') == 'browser_ax'), '')


def paper_action_error(action, obs, goal, route=None):
    """Reject an explicitly identifiable wrong paper, never invent a destination."""
    if not goal:
        return ''
    expected = goal['binding']['canonical_id']
    url = (route or {}).get('goto_url', '')
    if action.action == 'goto':
        url = action.args.get('url', '')
    elif action.action in {'click', 'double_click', 'right_click'}:
        ref = (route or {}).get('target_ref') or action.args.get('target_ref')
        eid = action.args.get('element_id')
        matches = [n for n in exposed_nodes(obs) if (ref and n.get('ref') == ref) or (eid and n.get('id') == eid)]
        if len(matches) == 1:
            url = urljoin(observation_url(obs), matches[0].get('href') or '')
    identifier = arxiv_id_from_url(url)
    if identifier and not same_paper(identifier, expected):
        return f'目标链接属于 arXiv {identifier}，任务已绑定 {expected}；请使用已核实论文的链接。'
    current = arxiv_id_from_url(observation_url(obs))
    keys = {str(k).lower() for k in (action.args.get('keys') or [])} if action.action == 'hotkey' else set()
    save_shortcut = action.action == 'hotkey' and bool(keys & {'ctrl', 'control', 'cmd', 'command'}) and 's' in keys
    if (current and not same_paper(current, expected) and action.action in {'click', 'double_click', 'right_click', 'hotkey', 'press'}
            and (re.search(r'download|save|下载|保存', str(action.args), re.I)
                 or save_shortcut)):
        return f'当前页面属于 arXiv {current}，不是已绑定的 {expected}；先返回正确论文页面，不要下载当前文件。'
    return ''


def paper_download_preflight_error(action, obs, completion, route=None):
    """Require a verified paper binding and matching official PDF before transfer.

    Browsing a questionable result is allowed for inspection. The irreversible
    boundary is the download action, where a wrong file must be stopped before
    dispatch instead of detected after it lands on disk.
    """
    if obs is None or completion is None or not completion.paper_goals():
        return ''
    route = route or {}
    ref = str(route.get('target_ref') or action.args.get('target_ref') or '')
    eid = str(action.args.get('element_id') or '')
    matches = [node for node in exposed_nodes(obs)
               if (ref and str(node.get('ref')) == ref) or (eid and str(node.get('id')) == eid)]
    node = matches[0] if len(matches) == 1 else {}
    if action.action == 'press' and str(action.args.get('key') or '').casefold() in {'enter', 'return'}:
        focused = [item for item in exposed_nodes(obs) if (item.get('states') or {}).get('focused')]
        if len(focused) == 1:
            node = focused[0]
    current_url = observation_url(obs)
    target_url = (str(action.args.get('url') or '') if action.action == 'goto' else
                  urljoin(current_url, str(node.get('href') or '')) if node.get('href') else '')
    text = ' '.join((str(action.args.get('target') or ''), str(node.get('name') or '')))
    path = urlsplit(target_url).path.casefold() if target_url else ''
    pdf_link = bool(target_url and ('/pdf/' in path or path.endswith('.pdf')))
    save_keys = {str(key).casefold() for key in action.args.get('keys', [])} if action.action == 'hotkey' else set()
    save_shortcut = action.action == 'hotkey' and 's' in save_keys and bool(
        save_keys & {'ctrl', 'control', 'cmd', 'command', 'meta'})
    named_download = action.action in {'click', 'double_click', 'right_click'} and bool(
        re.search(r'\b(?:pdf|download|save)\b|下载|保存|导出', text, re.I)
        or node.get('download'))
    enter_download = action.action == 'press' and bool(node.get('href')) and pdf_link
    viewer_download = save_shortcut and (
        '/pdf/' in urlsplit(current_url).path.casefold()
        or (obs.info or {}).get('document_kind') == 'pdf')
    if not (action.action == 'download' or pdf_link or named_download or enter_download or viewer_download):
        return ''
    goals = completion.paper_goals()
    if len(goals) != 1 or not goals[0].get('binding'):
        return ('尚未从官方论文详情页核实目标身份和 PDF 地址；可以打开候选详情页检查，'
                '但不能先下载再判断文件是否正确。')
    binding = goals[0]['binding']
    expected = binding['canonical_id']
    target_id = arxiv_id_from_url(target_url) if target_url else None
    current_id = arxiv_id_from_url(current_url)
    if target_url and (not target_id or not same_paper(target_id, expected)):
        return ('下载目标不是已核实的官方 PDF；请使用框架已核实的地址 '
                + binding['pdf_url'])
    if not target_url and (not current_id or not same_paper(current_id, expected)
                           or '/pdf/' not in urlsplit(current_url).path.casefold()):
        return ('当前页面不能证明下载的是已核实论文；请先打开 '
                + binding['pdf_url'] + ' 再下载。')
    return ''


def search_navigation_error(action, task, step, obs, completion=None):
    """A paper URL must come from the task, observed links or trusted binding.

    With a completion verifier this applies across every paper phase, including
    identify. The optional argument preserves the legacy search-only entry point
    and leaves generic non-paper tasks outside this guard.
    """
    if action.action != 'goto':
        return ''
    goals = completion.paper_goals() if completion is not None else []
    if completion is not None:
        if not goals:
            return ''
    elif (step or {}).get('phase') != 'search':
        return ''
    url = action.args.get('url', '')
    parsed = urlsplit(url)
    if (goals and (step or {}).get('phase') == 'search'
            and parsed.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}
            and parsed.path.split('/')[1:2] in [['submit'], ['login'], ['register']]
            and not re.search(r'登录|账户|账号|提交|上传|login|account|submit|upload', task, re.I)):
        from .paper_sources import paper_search_instruction
        return ('当前目标是检索公开论文，提交/登录页面不提供本次所需入口；请按来源策略进入搜索页，按用户原名称检索。'
                + paper_search_instruction(task))
    identifier = arxiv_id_from_url(url)
    if not identifier or identifier in task or url in task:
        return ''
    if any(goal.get('binding') and same_paper(identifier, goal['binding']['canonical_id']) for goal in goals):
        return ''
    observed = [observation_url(obs)]
    for block in browser_context(obs):
        observed.extend(n.get('href', '') for n in block.get('nodes', []) if isinstance(n, dict))
    observed.extend(c.get('url', '') for c in (getattr(obs, 'info', {}) or {}).get('download_candidates', [])
                    if isinstance(c, dict))
    if any(arxiv_id_from_url(candidate) == identifier for candidate in observed):
        return ''
    return '当前任务要求按论文名称检索；尚未在当前观察或原始任务中看到此 arXiv ID，也没有对应可信论文绑定，不能用记忆中的网址跳过搜索或身份查找。'


def operational_facts(obs, completion, report, step=None, *, os_name="", search_recovery=None):
    """Only public binding + browser event evidence, never model-written facts."""
    lines = workspace_facts(obs)
    selected = ((getattr(obs, 'info', None) or {}).get('decision_mode') or {})
    if (observation_scene(obs).get('reason') == 'content_without_actionable_ax'
            and selected.get('source') != 'jev'):
        lines.append('页面正文有大量可见内容，但 AX 只暴露少量导航或筛选控件。'
                     '目标内容卡片请根据当前浏览器截图做视觉坐标点击，进入详情后重新观察下载入口；'
                     '已有明确 ref 的导航/输入控件仍可用 DOM。不要重复点击当前已选中的导航标签。')
    inspecting = (step or {}).get('phase') == 'inspect_download'
    if inspecting and completion:
        report = {'checks': completion.download_progress()}
    goal = active_paper(completion, step)
    if goal:
        binding = goal['binding']
        lines.append(f"已核实论文：{binding['full_title']}；arXiv {binding['canonical_id']}。")
        lines.append(f"已核实 PDF 地址：{binding['pdf_url']}；可用 goto 打开后通过界面保存。")
        if urlsplit(observation_url(obs)).hostname == 'export.arxiv.org':
            lines.append('当前位于 arXiv 官方 export 镜像；可在同一镜像打开 /pdf/'
                         + binding['canonical_id'] + '，下载产物仍由框架核验论文身份。')
        if goal.get('filename'):
            lines.append('交付文件名：' + goal['filename'])
        current = arxiv_id_from_url(observation_url(obs))
        if current and not same_paper(current, binding['canonical_id']):
            lines.append('当前打开的是其他论文，请回到上述已核实地址。')
    elif completion and urlsplit(observation_url(obs)).path.startswith('/search/'):
        goals = completion.paper_goals()
        if len(goals) == 1 and goals[0]['status'] == 'pending':
            cards = [card for block in browser_context(obs) for card in paper_result_cards(block)]
            url = urlsplit(observation_url(obs))
            searchtype = parse_qs(url.query).get('searchtype', [''])[0].casefold()
            title_selected = any(n.get('id') == 'searchtype' and str(n.get('value') or '').strip().casefold() == 'title'
                                 for block in browser_context(obs) for n in block.get('nodes', []))
            if title_selected and searchtype != 'title':
                lines.append('上方搜索表单已选 Title，但结果 URL 仍不是标题检索；必须点击上方搜索框右侧的 Search 按钮提交。不要再次选择 Title，也不要点击下方排序表单的 Go。')
    page_url = observation_url(obs)
    if completion and is_scholar(page_url):
        goals = completion.paper_goals()
        if len(goals) == 1 and goals[0]['status'] == 'pending':
            query = goals[0]['query']
            params = parse_qs(urlsplit(page_url).query)
            if params.get('hl', [''])[0].lower() != 'en':
                lines.append('Google Scholar 当前界面语言不是英语；可打开从当前页面 URL 仅修改 hl=en 得到的地址：'
                             + english_scholar_url(page_url))
    if completion:
        goals = completion.paper_goals()
        if len(goals) == 1 and goals[0]['status'] == 'pending':
            from .paper_search import PaperSearchRecovery
            recovery = search_recovery if search_recovery is not None else PaperSearchRecovery()
            facts = recovery.facts(obs, goals[0]['query'])
            lines.extend(facts)
            if not facts:
                # Result-only snapshots can lack the input control. Their
                # titles are still useful, but cannot justify a fabricated ref.
                rows = [card for block in browser_context(obs) for card in paper_result_cards(block)]
                rows.extend(item for block in browser_context(obs) if is_scholar(block.get('url', ''))
                            for item in scholar_title_links(block))
                matches = [r for r in rows if query_matches_title(goals[0]['query'], r['title'])]
                if matches:
                    lines.append('当前结果存在名称字面命中的候选；这只是检索提示，不证明身份，也不排除其他结果。'
                                 '结合标题、摘要和项目说明判断相关性，可打开实际观察到的候选详情；下载前仍须核实身份。')
                elif rows:
                    lines.append(f'当前结果页读取 {len(rows)} 条标题，尚无名称字面命中；这不表示候选不相关，也不能证明论文不存在。'
                                 '先结合标题、摘要和项目说明作语义判断，相关候选可直接打开详情核实；没有合适候选再调整查询或入口。'
                                 '只有已观察到完整标题且需要缩小范围时才考虑双引号；不要重填同一查询或猜作者/ID。')
    info = (obs.info or {}) if obs else {}
    if (urlsplit(observation_url(obs)).hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}
            and any(str(n.get('name', '')).strip().lower() == 'rate exceeded.'
                    for block in browser_context(obs) for n in block.get('nodes', []))):
        lines.append('arXiv 当前页面明确显示 Rate exceeded；不要重复刷新或重复访问同一搜索端点。'
                     '请换一个搜索入口，或在普通搜索引擎按用户给出的原标题搜索，再点击实际结果中观察到的 arXiv 官方链接。'
                     '不可凭记忆中的 ID 跳过搜索；若搜索入口均不可用，应报告外部网站限流。')
    if info.get('active_surface') == 'desktop':
        from .desktop_readiness import foreground, window_ready
        import json
        lines.append('当前为桌面接管：截图覆盖虚拟桌面，界面输入会使用真实鼠标键盘。返回后台网页操作用 observe_browser。')
        front = foreground(info.get('runtime_state', {}))
        if front:
            lines.append('系统观察到的前台窗口：' + str(front.get('process_name', ''))[:80] + ' / '
                         + json.dumps(str(front.get('title', ''))[:160], ensure_ascii=False)
                         + ('；窗口有响应，内容加载需另行核查。' if window_ready(front) else '；尚未确认可接收输入。'))
        waiting = info.get('desktop_window_wait') or {}
        if waiting.get('status') in {'timeout', 'cancelled', 'task_timeout'}:
            lines.append('最近窗口等待结果：' + str(waiting['status']) + '；先核对当前前台窗口，不能盲发 Ctrl+L 或重复启动。')
    pending = info.get('pending_downloads', [])
    if pending:
        lines.append('系统仍有下载进行中；等待框架确认，不要重复点击下载。')
    artifacts = [a for a in info.get('artifacts', []) if isinstance(a, dict) and a.get('completed') is True]
    for artifact in artifacts[-3:]:
        path = str(artifact.get('path') or artifact.get('filename') or artifact.get('suggested_filename') or '已登记文件')
        size = artifact.get('bytes')
        lines.append('下载传输已完成：' + path[:400] + (f'（{size} 字节）' if type(size) is int else '') + '；这不等于任务验收通过。')
        ids = [arxiv_id_from_url(artifact.get(k, '')) for k in ('url', 'final_url')]
        if goal and any(i and not same_paper(i, goal['binding']['canonical_id']) for i in ids):
            lines.append('该文件来源属于其他论文；不要继续点击它的下载按钮，也不要将它改名冒充目标论文。')
    # Use public_report; private evaluator values must never enter policy input.
    for check in (report or {}).get('checks', []):
        if check.get('kind') not in {'paper_download', 'download'}:
            continue
        reason = check.get('reason', '')
        if reason == 'download transport does not identify the bound official paper':
            lines.append('文件验收未通过：下载来源与已绑定论文不符，需打开正确 PDF 地址重新保存。')
        elif reason == 'downloaded filename does not match':
            lines.append('文件已收到，文件名仍不符合要求；CLI 可用时使用 cli rename（path为真实下载路径，destination为要求文件名）；否则通过文件管理器处理，不要再次下载。')
        elif artifacts and check.get('status') != 'passed':
            lines.append('文件验收仍待处理：' + str(reason)[:180])
    if artifacts and not pending:
        lines.append('请根据文件验收反馈推进后续步骤，不要仅因截图没有下载提示而重复下载。')
    rendering = info.get('render_wait', {})
    if rendering.get('status') == 'timeout':
        lines.append('PDF 渲染等待已超时，当前画面尚未确认就绪；请检查页面或切换观察，不要在空白画面反复保存。')
    if not inspecting and not info.get('cli_capability', {}).get('available'):
        lines.extend(rename_ui_guidance(obs, completion, report, os_name))
    else:
        lines.append('下载文件的存在性、完整性及 PDF 身份由框架直接检查；不要打开文件管理器核查。'
                     '若框架报告文件错误，再通过浏览器重新获取正确 PDF。')
    return list(dict.fromkeys(lines))


def paper_review_error(obs, completion, report, step):
    """A screenshot reviewer cannot overrule a known paper-identity conflict."""
    goal = active_paper(completion, step)
    if not goal:
        return ''
    phase = step.get('phase')
    if phase in {'identify', 'locate_fulltext', 'download'}:
        current = arxiv_id_from_url(observation_url(obs))
        expected = goal['binding']['canonical_id']
        if current and not same_paper(current, expected):
            return f'当前页面 arXiv {current} 与已绑定目标 {expected} 不符，不能确认本步骤完成。'
    if phase in {'download', 'verify'}:
        for check in report.get('checks', []):
            if (check.get('id') == goal['check_id'] and check.get('status') == 'failed'
                    and check.get('reason') == 'download transport does not identify the bound official paper'):
                return '下载文件的来源与已绑定论文不符，不能确认下载步骤完成。'
    return ''


def rename_ui_guidance(obs, completion, report, os_name):
    """Describe a GUI route only for a verifier-selected registered artifact.

    No file lookup or mutation happens here. Paths come from browser events and
    must agree with the public verifier's chosen artifact, not model memory.
    """
    if completion is None or os_name.lower() != 'windows':
        return []
    from pathlib import PureWindowsPath
    import json
    public = {c['id']: c for c in completion.public_spec.get('checks', [])}
    artifacts = [a for a in (obs.info or {}).get('artifacts', [])
                 if isinstance(a, dict) and a.get('completed') is True] if obs else []
    pairs = []
    for check in (report or {}).get('checks', []):
        spec = public.get(check.get('id'), {})
        if (check.get('status') != 'failed' or check.get('reason') != 'downloaded filename does not match'
                or not spec.get('filename')):
            continue
        path = check.get('path')
        if not isinstance(path, str) or not PureWindowsPath(path).is_absolute() or any(ord(c) < 32 for c in path):
            continue
        matching = [a for a in artifacts if isinstance(a.get('path'), str)
                    and PureWindowsPath(a['path']) == PureWindowsPath(path)]
        if len(matching) == 1:
            pairs.append((PureWindowsPath(path), spec['filename']))
    if len(pairs) != 1:
        return []
    path, name = pairs[0]
    info = obs.info or {}
    lines = ['当前文件需要界面改名：' + json.dumps(path.name, ensure_ascii=False)
             + ' → ' + json.dumps(name, ensure_ascii=False) + '。无需再次下载。']
    if info.get('active_surface') != 'desktop':
        lines.append('先 observe_desktop 获取桌面截图，再操作资源管理器。')
    foreground = next((a for a in info.get('runtime_state', {}).get('applications', []) if a.get('foreground')), {})
    explorer = (str(foreground.get('process_name', '')).casefold() == 'explorer.exe'
                and foreground.get('class_name') in {'CabinetWClass', 'ExploreWClass'})
    lines.append('资源管理器已在前台，继续定位文件，不要再次 switch_applications。' if explorer
                 else '若资源管理器尚未打开，用 hotkey keys=["win","e"] 打开；不要重复切换不存在的窗口。')
    lines.append('按新截图逐步操作：资源管理器地址栏 Ctrl+L，输入下载目录 '
                 + json.dumps(str(path.parent), ensure_ascii=False) + ' 并 Enter；选中上述原文件后按 F2，编辑名称并 Enter 确认。')
    lines.append('注意扩展名：若重命名框只显示名称主体，保留系统隐藏的扩展名，避免生成双重 .pdf；显示完整文件名时填写完整目标名。最终仍以框架文件验收为准。')
    return lines


def workspace_facts(obs):
    """Paths are framework-owned episode metadata, never planner guesses."""
    state = (obs.info or {}).get('initial_state', {}) if obs else {}
    path = state.get('workspace_dir') or state.get('downloads_dir')
    if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
        return []
    import json
    remote = state.get('workspace_location') == 'controller_host'
    lines = [('框架产物目录（控制器本机）' if remote else '本次任务默认操作目录') + '：' + json.dumps(path, ensure_ascii=False)]
    if remote:
        lines.append('此目录不保证在远程桌面可见；未配置共享目录时不要把控制器路径输入远程文件管理器。')
    else:
        lines.append('下载、查找、改名和另存默认在此目录；只有原始任务明确指定其他文件或路径时才改用指定位置。')
    return lines


def download_action_url(action, obs, route=None):
    """Only an identifiable download/save intent, never arbitrary GUI coordinates."""
    if action.action not in {'click', 'double_click', 'hotkey', 'press'} or obs is None:
        return ''
    info = obs.info or {}
    keys = {str(k).lower() for k in (action.args.get('keys') or [])} if action.action == 'hotkey' else set()
    intent = (bool(keys & {'ctrl', 'control', 'cmd', 'command'}) and 's' in keys
              or bool(re.search(r'\bdownload\b|\bsave\b|下载|保存', str(action.args.get('target', '')), re.I)))
    if not intent:
        return ''
    if info.get('active_surface') == 'desktop':
        foreground = next((a for a in info.get('runtime_state', {}).get('applications', []) if a.get('foreground')), {})
        if str(foreground.get('process_name', '')).casefold() not in {'chrome.exe', 'msedge.exe', 'firefox.exe'}:
            return ''  # A file-manager rename confirmation is not a browser download.
    ref = (route or {}).get('target_ref') or action.args.get('target_ref')
    eid = action.args.get('element_id')
    nodes = [n for n in exposed_nodes(obs) if (ref and n.get('ref') == ref) or (eid and n.get('id') == eid)]
    if len(nodes) == 1 and nodes[0].get('href'):
        return urljoin(observation_url(obs), nodes[0]['href'])
    return observation_url(obs) or next((t.get('url', '') for t in info.get('tabs', []) if t.get('active')), '')
