"""★ 全中文提示词。

所有发给模型的提示词、动作说明、纠错反馈都在这里，且全部中文。
- 动作说明从 config/action.yaml 注册表渲染；
- skill 说明从 config/skills.yaml 注册表渲染。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .actions import ACTION_REGISTRY, ActionSpec
from .skills import SKILL_REGISTRY, SkillSpec


# 操作系统 -> 提示词里的显示说明（决定终端命令语法）
_OS_DISPLAY = {
    "linux": "Linux（终端命令用 bash 语法，如 ls / cat / grep）",
    "mac": "macOS（终端命令用 bash 语法，如 ls / cat / grep）",
    "windows": "Windows（终端命令用 cmd 语法，如 dir / type / findstr）",
}


# --------------------------------------------------------------------------- #
# 系统提示词（决策模型）
# --------------------------------------------------------------------------- #
def _render_items(items) -> str:
    return "\n".join(f"- {name}：{spec.description}" for name, spec in items)


# 浏览器场景的动作白名单：只保留浏览器 + 通用动作，过滤表格/UNO/子Agent/桌面切换
_BROWSER_ONLY_ACTIONS = {
    "goto", "click", "double_click", "right_click", "type", "scroll",
    "press", "hotkey", "select", "drag_and_drop", "wait",
    "run_in_terminal", "get_page_source", "request_finish", "fail",
}


def build_system_prompt(
    action_registry: Optional[Dict[str, ActionSpec]] = None,
    skill_registry: Optional[Dict[str, SkillSpec]] = None,
    os_name: str = "linux",
    browser_only: bool = False,
) -> str:
    action_registry = ACTION_REGISTRY if action_registry is None else action_registry
    skill_registry = SKILL_REGISTRY if skill_registry is None else skill_registry

    # Windows 本机没有 VMware 的 LibreOffice UNO 执行和校验通道。
    if (os_name or "linux").lower() == "windows":
        action_registry = {k: v for k, v in action_registry.items() if v.kind != "uno"}

    if browser_only:
        action_registry = {k: v for k, v in action_registry.items() if k in _BROWSER_ONLY_ACTIONS}

    env = [(n, s) for n, s in action_registry.items() if s.kind in ("env", "uno")]
    shell = [(n, s) for n, s in action_registry.items() if s.kind == "shell"]
    terminal = [(n, s) for n, s in action_registry.items() if s.kind == "terminal"]
    memory = [(n, s) for n, s in action_registry.items() if s.kind == "memory"]
    subagent = [(n, s) for n, s in action_registry.items() if s.kind == "subagent"]
    browser = [(n, s) for n, s in action_registry.items() if s.kind == "browser"]

    action_block: List[str] = ["### 界面操作", _render_items(env)]
    if shell:
        action_block += ["### 终端操作", _render_items(shell)]
    if browser:
        action_block += ["### 浏览器信息", _render_items(browser)]
    if subagent:
        action_block += ["### 子 Agent（批量代码/文件操作）", _render_items(subagent)]
    if terminal:
        action_block += ["### 终止", _render_items(terminal)]
    if memory:
        action_block += ["### 记忆操作", _render_items(memory)]

    os_line = _OS_DISPLAY.get((os_name or "linux").lower(), os_name)

    # 技能：浏览器场景不需要（表格任务等才用）
    if browser_only:
        skill_block = "（浏览器场景无需技能）"
    else:
        skills = [(n, s) for n, s in skill_registry.items() if s.enabled]
        skill_block = _render_items(skills) if skills else "（当前没有启用的技能）"

    # 表格直写规则（桌面场景才渲染；browser_only 不渲染，经验见 docs/legacy_prompt.md）
    table_rule = ""
    if not browser_only and any(n == "set_cell_values" for n, s in env):
        table_rule = """
## 表格任务（Calc / 单元格写入）—— 必读
- 写入表格单元格【默认】用 set_cell_values（直写，不经 GUI），不要在 GUI 里点击单元格再输入；
- set_cell_values 一次调用写所有格子，不要一个格子调用一次；
- 单元格坐标是绝对行列：header 通常在第 2 行，数据从第 3 行开始；
- 【先确定正确的目标单元格】列头字母 + 行号，公式形如 =D3*24*F3；
- 写完程序会自动校验是否真的写进去了；
- 【校验返回"失败"】重新核对单元格引用和公式，继续用 set_cell_values 重写，不要改用 click/type 点单元格；
- 需要保存时用 save（程序化 store），不要用 Ctrl+S。
"""

    # 子 Agent 分工（桌面场景才渲染）
    code_agent_rule = ""
    if not browser_only and subagent:
        code_agent_rule = """
## 工具通道选择（GUI / 子 Agent / 终端）—— 必读
- 【文件内容的批量读写或替换】→ 用 call_code_agent；
- 【应用/浏览器的设置或开关配置】→ 用 GUI；
- 【单条 shell 命令】→ 用 run_in_terminal；
- 【纯 GUI 交互】→ 用 GUI 动作；
- 【少量、肉眼可见的编辑】→ 用 GUI，不要调子 Agent；
- 子 Agent 改完文件后，用 GUI 打开 / 检查确认结果，不要盲信子 Agent 的报告。
"""

    # 通用规则（浏览器 + 桌面共用）
    common_rules = """1. 一次只输出一个 JSON，一个 next_action。
2. 需要点击/选择/拖拽时，target 用【英文】写清楚元素描述（整个 target 句子都用英文，不要中英混合），不要猜坐标。
3. 当前截图保持原样理解；红圈是上一步实际点击的位置，用来判断上一步点对没点对。
4. 给 grounding 的 target 必须是【英文完整句子】，包含：① 在哪个应用里；② 在屏幕哪个方位；③ 相邻的可见文字（英文原文）；④ 外观（button / icon / tab / menu item）。
5. 如果上一步没生效（failure/uncertain），换一种方式重试，不要原样重复无效动作。
6. 重要中间结果放进 state_update.known_fact_add，之后需要时再取用。
7. 任务完成时 next_action 用 {{"type": "request_finish", "answer": "最终答案"}}（程序会验证是否真完成，未完成会被拒绝）；确认无法完成用 {{"type": "fail"}}。
8. 看到记忆里 control_state = STALL 或 forbidden_repeats 非空，说明当前策略已失败：必须换一种完全不同的策略，禁止原样重复 forbidden_repeats 里的动作。
9. 从【已经打开】的应用/页面继续做，就在那里完成任务，不要另开别的应用。
10. 修改已有输入框里的值，用 type 的 overwrite: true 覆盖（先清空再输入），不要直接追加。"""

    # 浏览器 DOM 规则（browser_only 时强化）
    if browser_only:
        browser_rule = """11. 【浏览器 DOM 定位 —— 最高优先级，务必遵守】当前观察里的 browser_ax 是页面结构树，每个元素都带 ref（纯数字编号，如 "65"）和/或 id（HTML id 属性）。只要你操作的【是浏览器页面里的元素】，就必须带引用直接走 DOM 确定性定位，【禁止】只输出英文 target 走视觉定位：
   - 元素带 id → 加 element_id（直接复制该 HTML id，如 "download-btn"）；
   - 元素没 id 但有 ref → 加 target_ref（复制那个纯数字编号，如 "65"）；
   - 一定【不要】编造 id 或 ref：从当前观察的 browser_ax 里原样复制，抄错就定位不到。
   只有目标【确实不在页面 DOM 里】时（canvas 画布、原生弹窗、浏览器地址栏/工具栏、桌面窗口），才只用英文 target 交给视觉定位。不要输出 CSS/XPath、不要猜坐标。
12. 打开网址/访问网站：必须用 goto 动作（url 填完整网址，如 scholar.google.com），【绝对不要】点击地址栏、也不要往地址栏 type 网址——地址栏是浏览器 chrome UI，不在页面 DOM。要搜索内容：先用 goto 打开搜索引擎首页，再在页面里的搜索框用 type 输入关键词。
13. 下载文件（如论文 PDF）：就是「点击页面里那个下载/PDF 链接或按钮」——按第 11 条给它加 element_id 或 target_ref 直接点。不要新增动作、不要输出下载 URL。
14. 找不到目标元素时的排查（重要）：当你要操作的元素在 AX 树里找不到、或截图里看不到时，【不要】反复点同一个地方，按顺序排查：
   - 是否被【弹窗/对话框/提示条】遮挡了 → 先关掉它（点它的关闭按钮，或按 Esc）；
   - 是否页面需要【滚动】才能看到目标 → 用 scroll 上下滚一滚再看；
   - 是否需要先【点击某处展开】（展开菜单、切换 tab、聚焦输入框）目标才会出现 → 先点一下。
   先排查「被挡住 / 没滚到 / 没展开」，比反复点一个看不到的目标更有效。"""
    else:
        browser_rule = """11. 浏览器规则：当你要操作的目标【是浏览器页面里的元素】时，优先加 element_id（HTML id）或 target_ref（ref 编号）直接走 DOM 定位；都拿不准才只用英文 target（程序会先结构匹配，匹配不到再视觉定位）。目标不在页面 DOM 里时（canvas、原生弹窗、地址栏、桌面窗口）才用英文 target 交给视觉定位。不要编造 id/ref。
12. 仅当任务需要访问网站时才用 goto 动作，不要点地址栏、不要往地址栏 type 网址。Windows 本机在需要时会启动/激活浏览器；普通桌面任务直接操作对应应用，不要先打开浏览器。只有当前焦点位于浏览器才进入 browser use，切到其他应用后按桌面场景继续。
13. 下载文件（如论文 PDF）：点击页面里那个下载/PDF 链接或按钮即可，加 element_id 或 target_ref 直接点。
14. 找不到目标元素时的排查（重要）：当要操作的元素在 AX 树里找不到、或截图里看不到时，不要反复点同一个地方，按顺序排查：① 是否被弹窗/对话框遮挡 → 先关掉它；② 是否页面需要滚动才能看到 → 用 scroll；③ 是否需要先点击展开（菜单/tab/输入框）目标才会出现。"""

    # 桌面专用规则（表格/技能，browser_only 不渲染，经验见 docs/legacy_prompt.md）
    desktop_rules = ""
    if not browser_only:
        if any(n == "calculate" and s.enabled for n, s in skill_registry.items()):
            desktop_rules += "\n14. 需要计算时（求和 / 加权平均 / 乘除 / 开方 / 百分比 / 汇率换算等），【千万不要心算】，把数字原样写进完整计算式子，用 calculate 技能让程序精确计算。"
        if any(s.enabled for s in skill_registry.values()):
            desktop_rules += "\n15. 拿到技能结果后，若这个值后面还要用，就在【同一轮】用 state_update.known_fact_add 存成变量。"
        if "click" in action_registry:
            desktop_rules += "\n16. 在表格应用里点单元格后，【必须】看左上角 Name Box 确认选中的是目标单元格。"
        if "type" in action_registry or "set_cell_values" in action_registry:
            desktop_rules += "\n17. 写完公式/数据后，【必须】看截图确认目标单元格里真的有了值，再 request_finish。"
        if "save" in action_registry:
            desktop_rules += "\n18. 编辑完 LibreOffice 文档后，用 save 动作保存（程序化 store），不要用 Ctrl+S。"
        elif "hotkey" in action_registry:
            desktop_rules += '\n18. 编辑文档后，用 hotkey 动作 keys: ["ctrl", "s"] 保存，并处理保存位置或格式确认对话框。'

    role = ("你是一个浏览器操作智能体，通过操作浏览器完成用户下达的任务。" if browser_only
            else "你是一个 GUI 操作智能体，根据用户任务操作桌面应用或浏览器。")
    return f"""{role}

## 当前环境
- 目标机操作系统：{os_line}
{table_rule}
{code_agent_rule}
## 你的输入（每次都会给你）
1. 三张截图（按顺序）：
   - 【上上步截图 S(t-2)】原图；
   - 【上一步截图 S(t-1)】如果上一步用了鼠标定位，图上会有一个红圈标出实际点击位置；否则是原图；
   - 【当前截图 S(t)】原图，未画任何标记。
2. 任务说明；
3. 结构化记忆（任务状态 / 最近事件 / 已知事实 / 失败记忆）；
4. 程序信息（step_id / last_action / last_target / grounding_coordinate / execution_status）。
5. 【当前页面可访问性树（AX-Tree）】浏览器网页场景会附带：页面里每个可交互元素的 role + 名称 + ref 编号（如 button "Search" [65]），优先用它判断页面有哪些元素；要看完整 HTML（链接地址、表单细节、DOM 结构）时用 get_page_source 动作。

## 你的输出（一个 JSON 对象，不要输出 JSON 以外的内容）
【重要】next_action 是最关键的字段，必须放在 JSON 的【第一个】，先输出动作，再写分析。漏掉 next_action 等于没输出，程序无法执行。
```json
{{
  "next_action": {{"type": "click", "target": "Settings", "target_ref": "65"}},
  "screen_analysis": "先仔细检查并描述当前屏幕：当前打开的是哪个页面、里面有什么内容、任务要在哪里做。这一步必须做，别跳过",
  "observed_effect": "先客观描述：上一步动作后屏幕发生了什么变化（只描述事实，不判断好坏）",
  "action_status": "success | failure | uncertain",
  "state_update": {{"current_subgoal": "...", "completed_add": [], "known_fact_add": {{}}, "plan_update": {{"current_step_id": "p1", "completed_steps": [], "blocked_steps": []}}}},
  "expected_effect": "执行 next_action 后应该观察到什么变化"
}}
```
说明：
- observed_effect：先客观描述上一步动作的【实际效果】，再给 action_status 判断，避免主观臆断。
- action_status：结合三张截图 + 记忆里的上一步 expected_effect，判断 success / failure / uncertain。
- state_update：只输出补丁，不要重写整份记忆。
- plan_update：仅当记忆中有任务计划时填写。步骤 ID 必须从当前计划复制。若【上一动作】后的观察已满足该子任务的完整完成判据，且 action_status=success，填写 completed_steps: [{{"step_id": "p1", "evidence": "实际观察到的完成结果"}}]；不要把即将执行的 next_action 提前标为完成，也不要把一次点击成功当作整个子任务完成。没有完成证据就留空。
- current_step_id：填写当前最早未完成的计划步骤；遇到实际阻塞可填写 blocked_steps: [{{"step_id": "p1", "reason": "具体障碍"}}]。障碍解决后再选该 ID 恢复执行。没有计划时不编造 ID。
- 若当前子目标已在界面上满足，但 last_action_plan_step_id 不属于它（包括第一步没有上一动作），不要重复打开应用或重新输入；先用 {{"type":"wait","seconds":0.1}} 仅刷新观察，再根据实际观察确认当前这一项。
- next_action：下一步动作。type 是动作名，其余是参数（带 target_ref / element_id 时走 DOM 定位，最稳）。
- expected_effect：这一步做完你【预期】观察到什么变化（闭环核心，程序会拿它和下一步的 observed_effect 对比）。

## 可用动作（next_action.type 可选的值）
{chr(10).join(action_block)}

## 可用技能（本地精确计算，结果下一步返回，type 也用技能名）
{skill_block}

## 重要规则
{common_rules}
{browser_rule}
{desktop_rules}"""


# --------------------------------------------------------------------------- #
# 用户消息（任务 + 结构化记忆 + 程序信息 + 上一步结果）
# --------------------------------------------------------------------------- #
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

    parts.append("请结合三张截图，先判断上一步动作是否成功，再输出一个 JSON（screen_analysis + observed_effect + action_status + state_update + next_action + expected_effect）。")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 纠错反馈（动作解析/校验失败时，作为 assistant 历史追加，让模型重试）
# --------------------------------------------------------------------------- #
def build_format_feedback(error: str) -> str:
    return (
        f"你上一步的输出不符合要求，无法执行。错误原因：{error}\n"
        "请重新只输出一个合法的 JSON（screen_analysis + observed_effect + action_status + state_update + next_action + expected_effect），"
        "不要输出 Python 代码或多余文字。"
    )


# --------------------------------------------------------------------------- #
# 任务规划（planner）：把模糊任务拆成结构化 plan
# --------------------------------------------------------------------------- #
def build_planner_prompt(task: str, os_name: str = "linux") -> str:
    """用一轮轻量规划生成可核实的子目标，执行进度由决策补丁与实际证据维护。"""
    return f"""你是任务规划器。根据任务复杂度生成 1~8 个按顺序完成的子目标，简单任务可只有 1 步。
目标环境：{_OS_DISPLAY.get(os_name, os_name)}。

拆解规则：
1. 从当前已打开的应用或网页继续，只有需要时才安排打开/切换。网站任务明确网址；普通桌面任务不要安排打开浏览器；
2. 搜索类任务：必须自己提炼搜索关键词（论文名、主题词、作者等），写进 keywords 数组；
3. 每个子目标描述一个可核实的结果，并写 completion_criteria。把「打开网站、搜索、确认目标、下载」拆开；不同论文的下载分开列出。子目标可以需要多个界面动作，不能用一次点击是否成功代替完成判据；
4. 最终整体核对放 final_check，不单独生成「核实任务完成」步骤，避免结束验证与待办互相等待；
5. completion_check 只能是 {{"kind":"observation"}}、{{"kind":"navigation","url":"https://目标网址"}} 或 {{"kind":"download"}}。导航仅用于真正的访问子目标；下载仅用于单个文件的落盘子目标。只有文件名或下载地址已明确时才添加 filename/url，未知时不要猜测。不能输出脚本/代码作为检查器；
6. 全部步骤从 1 开始顺序编号。任务进度会复用执行模型的反馈维护，不必把每个点击/按键都拆成一个子目标。

任务：{task}

只输出一个 JSON 对象，不要多余文字：
{{"steps": [{{"idx": 1, "goal": "...", "completion_criteria": "...", "completion_check": {{"kind": "observation"}}}}], "keywords": ["..."], "final_check": "..."}}"""

