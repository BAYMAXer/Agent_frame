"""规划和兼容提示入口；每轮决策模板见 decision_context，独立核查见 step_verifier。"""
from __future__ import annotations

from typing import Dict, List, Optional

from .actions import ACTION_REGISTRY, ActionSpec
from .paper_sources import paper_search_instruction


def build_system_prompt(action_registry=None, skill_registry=None, os_name="linux", browser_only=False) -> str:
    """Static compatibility contract for callers without an observation.

    Live Pipeline decisions use decision_context.system_sections with a fresh
    mode. Human recorders and the prompt-preview API still need the basic schema
    here; treating their absent observation as a capture failure would incorrectly
    describe every recorded click as an unavailable recovery-only action.
    """
    from .decision_context import CORE_PROMPT
    from .actions import BASIC_ACTIONS
    registry = ACTION_REGISTRY if action_registry is None else action_registry
    names = [n for n in registry if n in BASIC_ACTIONS]
    return (CORE_PROMPT + f"\n操作系统：{os_name}。当前未指定观察模式；实际决策按新观察生成模式与菜单。\n"
            + "基础动作格式（type / 必填参数）：\n"
            + "\n".join(f"- {n}({', '.join(registry[n].params)})" for n in names))


def build_mode_system_prompts(os_name="windows", browser_only=False):
    """Independent editable previews of the same policies used by Pipeline.

    A preview has no live target refs or task; actual calls add the current
    observation's capabilities and relevant task rules, never both mode bodies.
    """
    from .decision_context import action_scope, scoped_system
    from .decision_modes import DecisionMode
    profiles = {"dom": DecisionMode("dom", "browser", "template_preview"),
                "gui_browser": DecisionMode("gui", "browser", "template_preview"),
                "gui_desktop": DecisionMode("gui", "desktop", "template_preview"),
                "cli": DecisionMode("cli", "terminal", "template_preview")}
    return {key: scoped_system(action_scope(None, None, browser_only, profile=profile),
                               None, os_name, profile=profile)
            for key, profile in profiles.items()}


def build_editable_mode_prompts(os_name="linux", browser_only=False):
    """Only the policy body is editable; live menus are always generated afresh."""
    from .decision_context import action_scope, system_sections
    from .decision_modes import DecisionMode
    profiles = {"dom": DecisionMode("dom", "browser", "template_preview"),
                "gui_browser": DecisionMode("gui", "browser", "template_preview"),
                "gui_desktop": DecisionMode("gui", "desktop", "template_preview"),
                "cli": DecisionMode("cli", "terminal", "template_preview")}
    return {key: "\n".join(text for name, text in system_sections(
        action_scope(None, None, browser_only, profile=profile), None, os_name, profile=profile)
        if name != "actions") for key, profile in profiles.items()}


def build_user_prompt(
    task_instruction: str,
    memory_text: str,
    skill_results: Optional[Dict[str, str]] = None,
    terminal_result: str = "",
    code_agent_result: str = "",
    page_source: str = "",
    a11y_tree: str = "",
    browser_result: str = "",
) -> str:
    parts = [f"## 任务\n{task_instruction}"]

    parts.append(f"## 结构化记忆\n{memory_text if memory_text else '（初始，尚无记忆）'}")

    # 页面信息：浏览器场景才有，非浏览器为空。引导由 _page_a11y 按类型动态生成
    # （有 selector → 引导 click_element；无 selector 的 AX-Tree → 引导用 GUI），这里只做中性展示。
    if a11y_tree:
        parts.append(f"## 当前页面信息\n{a11y_tree}")

    extras: List[str] = []
    if skill_results:
        for name, val in skill_results.items():
            extras.append(f"- 上次技能 {name} 结果 = {val}")
    if terminal_result:
        extras.append(f"- 上次终端命令输出 = {terminal_result}")
    if code_agent_result:
        extras.append(f"- 子Agent结果：\n{code_agent_result}")
    if browser_result:
        extras.append(f"- 上次浏览器操作结果 = {browser_result}")
    if page_source:
        extras.append(f"- 当前页面源码：\n{page_source}")
    if extras:
        parts.append("## 上一步结果\n" + "\n".join(extras))

    parts.append("请结合当前观察，只输出一个 JSON：next_action 为本次动作，summary 用一句话说明观察与行动计划。执行结果由框架核查。")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 纠错反馈（动作解析/校验失败时，作为 assistant 历史追加，让模型重试）
# --------------------------------------------------------------------------- #
def build_format_feedback(error: str) -> str:
    return (
        f"本次候选动作未通过执行前校验，尚未执行：{error}\n"
        '请只修正本次输出，返回 {"next_action":{"type":"本轮动作名",...},"summary":"一句话观察与行动计划"}。'
        "保留原任务，不要把格式错误理解为上个已执行动作失败。"
    )


# --------------------------------------------------------------------------- #
# 模型验收器（LLM-as-judge）：对「无法写成确定性代码验收」的要求做模型判断保底
# --------------------------------------------------------------------------- #
def build_verification_prompt(task: str, criteria: str) -> str:
    """验收用提示词：由模型根据当前截图/页面结构判断要求是否已满足。"""
    return (
        "你是任务完成度验收器。请结合当前浏览器截图和页面结构，客观判断下面的要求是否已经满足。\n"
        "\n"
        f"任务：{task}\n"
        f"待验收要求：\n{criteria}\n"
        "\n"
        "判断原则：只有截图或页面里能【明确看到】要求已满足时才判 true；"
        "拿不准、看不到证据、或只是模型自己声称完成了，都判 false。\n"
        "\n"
        "只输出一个 JSON 对象，不要输出任何其他文字：\n"
        '{"success": true, "reason": "简要说明你看到了什么证据"}\n'
        "\n"
        "若未完成，输出：\n"
        '{"success": false, "reason": "简要说明缺什么"}'
    )


# --------------------------------------------------------------------------- #
# 任务规划（planner）：把模糊任务拆成结构化 plan
# --------------------------------------------------------------------------- #
def build_planner_prompt(task: str, completion_spec=None, profile=None) -> str:
    """Plan observable milestones; action-level decisions happen at execution time."""
    import json
    contract = json.dumps(completion_spec or {"checks": []}, ensure_ascii=False)
    workflow = json.dumps(profile or {}, ensure_ascii=False)
    phases = [item['phase'] for item in (profile or {}).get('workflow', []) if item.get('phase')]
    phase_rule = ('本任务必须按顺序包含这些 phase：' + ' → '.join(phases) + '；最后的 verify 也不可省略。') if phases else ''
    from .actions import BASIC_ACTIONS
    menu = ", ".join(n for n in ACTION_REGISTRY if n in BASIC_ACTIONS)
    has_checks = bool((completion_spec or {}).get('checks'))
    download_rule = ('下载落盘使用 download_complete(check_id)，由框架核对对应产物' if has_checks else
                     '本任务没有系统 check_id；下载落盘使用 artifact_download(format)，format 为 ppt/pdf/any，框架核对本轮下载事件与落盘文件，check_ids 必须为 []')
    from .task_specs import paper_download_intent
    paper_task = ((profile or {}).get('kind') == 'paper_download' or paper_download_intent(task)
                  or any(c.get('kind') == 'paper_download' for c in (completion_spec or {}).get('checks', [])
                         if isinstance(c, dict)))
    source_rule = paper_search_instruction(task) if paper_task else ''
    return f"""你是任务规划器。不要添加用户未要求的文件名或改名步骤；未指定文件名时保留网站实际提供的文件名。导航子任务只要求到达已知入口并加载，不要给尚未访问的入口预设列表或控件；下一阶段再定位内容。按任务的自然阶段形成最少但完整的、可独立核查的子目标，数量由任务复杂度决定，不按固定数量凑步骤。
一个子目标可以通过多个动作实现；不要把点击输入框、输入关键词、按回车拆成三个规划步骤，应合为“搜索目标并显示结果”。
列表可能直接位于首页或当前页某个标题下，不一定有独立列表页。把目标写成“读取并核实目标列表”，不要强制“点击某栏目进入另一页面”；已经读到目标内容就继续核实，不额外寻找入口。
保留身份核对、下载、改名等不同结果边界；简单任务可以只有一个子目标，复杂任务必须覆盖全部要求。
网页文件任务按可独立观察的结果划分：进入目标区域、定位具体文件或详情附件、触发下载、核对已下载文件。不要把“进入方案库”和“定位一个可下载 PPT”捆成一个子目标；进入区域已成功但尚未找到文件时，应继续寻找文件，而不是再次点击同一入口。输入和提交仍可合为一次搜索子目标，不要按单次鼠标动作拆步骤。
导航到目标区域只证明导航，不证明已找到 PPT；URL 变化不等于附件可下载。导航谓词可以使用用户明确给出的 URL，或当前观察中真实链接的 href/targetHref 对应的明确目标 URL，不需要虚构 check_id；尚未观察到的未来地址不得猜测，未知时使用 visual 并写明区域就绪的观察证据。
文件列表只显示部分内容：当前视口没有 PPT，不能断言全库没有 PPT。优先使用实际可见的站内搜索、类型筛选，或进入相关卡片详情检查附件；不要仅凭卡片标题判断附件格式。连续点击已选入口或滚动没有新的可核实内容时，换搜索、筛选或详情路径，不能无依据反复导航、盲目滚动。
计划描述目标和必要前提，不预先编造尚未观察到的按钮、ref、搜索结果或论文身份；执行时依据真实反馈修订路径。即使你记得某篇著名论文的作者、年份或 arXiv ID，只要原始任务与当前已观察页面没有给出，就不能写进 goal、expected_evidence、verification、keywords 或 final_check；这些值只能在后续页面中读取、核对。
搜索结果的默认顺序可能是最新发表，不能规划“第一条就是目标”或要求逐页翻到目标；应允许按实际观察到的标题、作者、年份等缩小范围。候选相关性由模型结合标题、摘要、项目说明和用户意图判断，名称字符串不同不能阻止打开候选详情；语义确认后再由框架绑定官方身份和下载链接。只有原任务指定“第一条”时才可将排名作为目标。
用户论文名称可能是简称、译名或主题描述。先按用户原名称不加引号检索，不能强制完整标题等于用户名称；只有从页面观察到完整标题且需要缩小范围时才考虑双引号。没有合适候选时扩展原名称加paper或换允许入口；不要重填已存在的查询，不补造作者或ID。
{source_rule}

拆解规则：
1. 把打开正确网站/应用作为必要操作前提；先遵循任务的来源策略。仅当当前页面符合来源策略，或是调用方明确预载且不与用户指定来源冲突时，才复用其可用搜索框；不能仅因当前页有搜索框就改走其他来源。来源受限时不得引导换到其他网站。搜索子目标无需把每个启动操作单列为子目标；
2. 搜索类任务：先从用户原话提取论文名或主题词写进 keywords 数组；作者、年份、论文 ID 只有用户明确给出或当前页面已观察到时才可加入；
3. 每个子目标要具体（含明确的操作对象和预期结果），不要写"完成任务"这种空话。不得把“搜索名为X的论文”改成“找一篇与X主题相关的论文”；短名先搜索核实身份，不凭常识猜成另一个对象；
4. 最后给出「完成判据」final_check：一个可以客观核对的完成标志（如"downloads 目录下存在该论文的 PDF 文件"）。
5. 补全操作前提与基本步骤：先到正确网站，找到目标，再操作，最后核对结果。不能用模糊的“找到并完成”代替操作步骤。
6. 每步写 expected_evidence，说明执行后应观察到的具体变化。goal 中写清本步所需的查询词、目标值、文件名等操作参数，不能只写“按要求填写”然后依赖 check_ids 代替指令；执行模型只看当前子目标。多对象任务逐个列出，不漏数量、筛选条件和保存要求。
7. 系统验收条件由调用方提供或由严格任务规则提取，你不能修改、删除或自行增加可信验收条件。check_ids 只引用下面已有 id；没有对应条件就填 []。
8. final_check 只是你的建议，不会作为任务完成的证据。任务结束由框架逐项核实。
9. 每个步骤增加 verification 数组：简单结果写明确谓词 url_equals(url)/url_host(host)/text_present(text)/app_open(app，真实进程名，不带路径)/element_state(name,role,states)；{download_rule}；改名/最终验收另列步骤，不能用截图猜下载成功。难以用这些条件完整核实的步骤用 {{"kind":"visual"}}。全部条件须同时满足，不能只检查最终目标的一小部分。例如“提交搜索且显示结果”不能仅验证在搜索网站域名。predicate 的期望值必须来自原始任务和明确计划目标，导航地址也可来自真实观察到的 href/targetHref；不允许使用“当前URL”等动态占位值。可选 actions 数组只列此步骤需要的基础交互动作。
CLI 规划格式示例（verification 只描述只读条件，不填写执行动作）：{{"goal":"写入 notes/data.txt 内容为指定文本","expected_evidence":"目标文件含指定文本","actions":["cli"],"verification":[{{"kind":"file_content","path":"notes/data.txt","text":"指定文本"}}]}}。目录存在也用 {{"kind":"file_exists","path":"notes"}}。不存在用 {{"kind":"file_absent","path":"notes/draft.txt"}}。核对两个文件内容完全一致用 {{"kind":"files_equal","path":"notes/data.txt","other_path":"notes/backup.txt"}}，由系统比较完整文件哈希。绝对不要写 verification.kind=cli/shell/write/mkdir/read；operation 只用于执行模型的 next_action，不属于计划 verification。难以代码判断的文件业务语义可用 visual，其核查输入会包含真实 CLI 结果，不仅是截图。
10. GUI/DOM 用于浏览器和桌面交互，下载仍点击真实链接。CLI 能力可用时，文件读写、改名、复制、移动和终端任务用 cli 动作，actions:["cli"]；相对路径基于默认工程目录。cli operation 为 list/stat/read/write/mkdir/copy/move/rename/open/shell/compare。文件结果可用 file_exists(path)/file_absent(path)/file_content(path,text)/files_equal(path,other_path) 确定性核查，内容及业务条件另行核实。CLI 不可用时用界面完成，不能在宿主机代替虚拟机操作。
11. 若给出类型化任务流程，steps 必须覆盖 workflow 中每个 phase（用相同英文值），每步 expected_evidence 不能为空。论文搜索先核对身份，再下载；不要提前编造下载URL。文件名要求放在 verify 阶段；框架会在 download 后自动插入独立的改名步骤，不必编造新的 phase。
12. 同时输出 requirements 数组，逐项保留原始任务要求。每项 kind 为 paper_download/rename/other，source_text 必须逐字引用任务中的连续原文；paper_download.value 为原文查询词，rename.value 为原文文件名，不能补造扩展名或论文ID。这些是候选解释，不是可信验收条件；框架会检查引用，最终仍核对完整原始任务。无法拆分的要求用 other 保留原文，不得省略否定、数量、目录或附加要求。
13. actions 使用以下精确动作名：{menu}。不要编造动作；CLI 文件操作必须在任务授权范围，不能用 shell 下载绕过浏览器下载记录。类型提示与验收契约独立：即使系统 checks 为空，也必须保留用户目标与对应流程。

verification 的 JSON 写法示例（仅示范字段，选择与本步目标相符的条件，不要整组照抄）：
- 打开某域名：{{"kind":"url_host","host":"example.org"}}
- 打开准确网址：{{"kind":"url_equals","url":"https://example.org/"}}
- 出现完整可见文字：{{"kind":"text_present","text":"示例完整标题"}}
- 打开应用：{{"kind":"app_open","app":"notepad"}}
- 激活应用：{{"kind":"app_foreground","app":"explorer"}}；打开后需继续键盘操作：{{"kind":"app_ready","app":"explorer"}}。app_open 仅证明窗口存在；app_ready 证明前台窗口有响应，不代表目录或业务内容已加载。Win+E 后由框架等待窗口就绪，超时先重新观察/选择窗口，不能盲发 Ctrl+L 或重复启动。
- 勾选指定控件：{{"kind":"element_state","name":"示例复选框","role":"checkbox","states":{{"checked":true}}}}
- element_state.states 只支持 checked、selected、expanded、disabled、focused 的布尔值；链接是否可见请用 text_present 或 visual，不要写 visible。
- 需要核对截图语义：{{"kind":"visual"}}
按示例写 host/url/text/app 等字段，不要统一写成 value；element_state 必须明确 name 与 states。
text_present 是完整可见文字匹配，不是关键词子串匹配；搜索短名不能证明论文身份，身份和全文结果需进一步核查。

系统验收条件：{contract}
任务流程模板：{workflow}
{phase_rule}

任务：{task}

只输出紧凑 JSON，不缩进、不换行、不解释。goal 和 expected_evidence 各用一句短句，保留必要对象、目标值与验收条件，避免复述任务。idx/status 由框架补齐；check_ids 为空、actions 无额外限制时省略这两个字段。不得为缩短输出遗漏任务要求。
{{"steps":[{{"phase":"search","goal":"...","expected_evidence":"...","verification":[{{"kind":"visual"}}]}}],"keywords":["..."],"final_check":"..."}}"""
