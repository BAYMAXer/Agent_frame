# 存档提示词（浏览器无关的经验，暂不用于 browser_only 提示词）

本文件保存从 system prompt 里抽掉的「表格 / 子 Agent / 技能」相关经验，
当前 browser_only 模式不渲染它们。之后做桌面应用 / OSWorld 表格任务时再启用。

---

## 表格任务（Calc / 单元格写入）—— 必读

- 写入表格单元格【默认】用 set_cell_values（直写，不经 GUI），不要在 GUI 里点击单元格再输入；
- set_cell_values 一次调用写所有格子（如 {"A3": "=SUM(A2:A10)", "B3": 5}），不要一个格子调用一次；
- 单元格坐标是绝对行列：header 通常在第 2 行，数据从第 3 行开始，写 E3 不是 E2；
- 【先确定正确的目标单元格】仔细看任务说明和表格，确定要写【哪个单元格】（列头字母 + 行号）。例如「total hours 是 time 格式、hourly rate 是 number」的任务，time 要先乘 24 转成小时数，公式形如 =D3*24*F3；
- 写完 set_cell_values 后，程序会自动校验目标单元格是否真的写进去了，不用你再确认；
- 【若校验返回"失败"】说明单元格引用或公式写错了：重新核对目标单元格（列头字母 + 行号）和公式，【继续用 set_cell_values 重写】，【绝对不要】改用 click / type 去点击单元格（视觉定位对表格单元格不可靠，容易点不中）；
- 需要保存时用 save（程序化 store），不要用 Ctrl+S。

## 工具通道选择（GUI / 子 Agent / 终端）—— 必读

- 【文件内容的批量读写或替换】→ 用 call_code_agent：把文档/表格里所有（或多处）某段文字替换成另一段（如「把文档里所有的 text 改成 test」）、批量读写 xlsx/docx/csv/pdf 内容、批量填表、数据清洗/排序/筛选。子 Agent 能用 Python（openpyxl / python-docx / sed / 字符串 replace）一步做完，不要一步步点 GUI 的查找替换对话框或菜单。
- 【应用/浏览器的设置或开关配置】→ 用 GUI：到设置页面直接点击/切换选项。不要用 call_code_agent 写脚本去改配置文件。
- 【单条 shell 命令】（kill / ps / ls / mkdir 等）→ 用 run_in_terminal，不要用 call_code_agent。
- 【纯 GUI 交互】（点击、导航、视觉布局、文件选择器）→ 用 GUI 动作。
- 【少量、肉眼可见】的编辑（几个格子、一次点击、一次菜单）→ 用 GUI（set_cell_values / click / type），不要调子 Agent（子 Agent 有发现文件 + 重载的开销，只对批量 / 替换 / 跨文件 / 需要程序逻辑的操作才划算）。
- 子 Agent 改完文件后，用 GUI 打开 / 检查确认结果，不要盲信子 Agent 的报告；不匹配就用 GUI 补做。

## 技能相关规则（已从 browser_only 规则里去掉）

- 需要计算时（求和 / 加权平均 / 乘除 / 开方 / 百分比 / 汇率换算等），【千万不要心算】，把从图上或页面读到的数字原样写进完整计算式子，用 calculate 技能让程序精确计算。
- 拿到技能结果后，若这个值后面还要用，就在【同一轮】用 state_update.known_fact_add 存成变量。
- 在表格（LibreOffice Calc）里点单元格后，【必须】看左上角 Name Box 确认选中的是目标单元格，不对就再点一次。
- 写完公式/数据后，【必须】看截图确认目标单元格里真的有了值，再 request_finish。
- 编辑完 LibreOffice 文档后，用 save 动作保存（程序化 store），不要用 Ctrl+S。
