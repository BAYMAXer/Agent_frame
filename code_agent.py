"""子 Agent（CodeAgent）：独立跑代码 / 文件操作的迷你 agent。

对齐原 Agent-S3 的 code_agent：
- 独立的对话循环 + 步数预算（默认 20 步）；
- 每步生成 python / bash / note 代码块，在目标机执行；
- 自己的 notes（key->value），跨步持久，最后随结果返回主 Agent；
- 返回结构化结果：task / steps / budget / completion_reason / summary / notes / execution_history。

用途：批量文件/数据操作（openpyxl 填表、unzip 解压、pdftotext 提取文本等），
避免主 Agent 一步步点 GUI 消耗步数。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

CODE_AGENT_PROMPT = """你是一个代码执行子 Agent，有有限的步数预算，通过逐步执行 Python / Bash 代码完成任务。

## 工作方式
- 增量逐步：每步只做一件小事，写完整、独立的代码片段（每步的代码不会跨步持久）。
- 用 sudo 时：echo password | sudo -S <命令>；用户名是 user。
- 执行后打印结果，出错就根据报错改下一步。
- 【重要】直接输出代码块或 DONE/FAIL，不要输出任何思考、解释、分析或重复内容。

## 输出格式（每步【只】输出一个代码块，或 DONE / FAIL）
Python 代码：
```python
你的代码
```
Bash 命令：
```bash
你的命令
```
【看图片】读取一张图片（照片/截图/扫描件）的内容，直接输出图片文件路径：
```view
/home/user/Desktop/receipt.png
```
存一条中间结果（供后续步骤和主 Agent 使用）：
```note
key=文件名
value=内容
```
任务完成（单独一行，不要代码块）：
DONE
任务无法用代码完成（单独一行）：
FAIL

## 关键原则
- 你能【看图】：要读取图片/截图/扫描件里的数据时，用 ```view 块输出文件路径（下一步图片会显示给你看），不要 pip install tesseract 装 OCR。
- 要读网页上的内容（Google 地图/搜索等 JS 渲染的页面），先用 pyautogui.screenshot 存成 PNG，再用 ```view 看，不要用 requests 抓（业务数据是 JS 渲染的，HTML 里没有）。
- 完成前，把所有主 Agent 后续需要的数据都存进 ```note 块（key=值）。
- 修改文件后，用 cat / print 打印最终内容验证。
- 只读文件用 unzip -p / python 标准库，别浪费步数 pip install 缺的包。
- 用 openpyxl/python-docx 改表格/文档时，直接改已打开的文件（原地改），别新建文件；改完 store 落盘。
"""


def _extract_code_block(text: str) -> Tuple[Optional[str], Optional[str]]:
    """从 code agent 输出里提取第一个 ```python/```bash/```note/```view 块。返回 (type, code)。"""
    m = re.search(r"```(python|bash|note|view)\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


def _parse_note(code: str) -> Tuple[str, str]:
    """解析 ```note 块里的 key=... / value=...。"""
    key, value = "", ""
    for line in code.splitlines():
        s = line.strip()
        if s.startswith("key="):
            key = s[4:].strip()
        elif s.startswith("value="):
            value = s[6:].strip()
    return key, value


class CodeAgent:
    """代码执行子 Agent。"""

    def __init__(self, model, env, budget: int = 20):
        self.model = model
        self.env = env
        self.budget = budget
        self.notes: Dict[str, str] = {}

    def _read_image(self, path: str):
        """读目标机里的图片文件，返回 bytes（供 view 块给模型看）。失败返回 None。"""
        ctrl = getattr(self.env, "controller", None)
        if ctrl is not None:
            get_file = getattr(ctrl, "get_file", None)
            if get_file is not None:
                try:
                    data = get_file(path)
                    if data:
                        return data
                except Exception:  # noqa: BLE001
                    pass
        return None

    def execute(self, task_instruction: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": CODE_AGENT_PROMPT},
            {"role": "user", "content": f"## 任务\n{task_instruction}\n\n请开始第一步。"},
        ]
        execution_history: List[Dict[str, str]] = []
        completion_reason = "BUDGET_EXHAUSTED"
        summary = ""
        no_code_streak = 0   # 连续没吐出代码块的次数，用于提前终止无效思考

        for _ in range(self.budget):
            raw = self.model.chat(messages)
            code_type, code = _extract_code_block(raw)

            if code_type is None:
                if re.search(r"^DONE\s*$", raw, re.MULTILINE):
                    completion_reason = "DONE"
                    summary = "子 Agent 认为任务已完成。"
                    break
                if re.search(r"^FAIL\s*$", raw, re.MULTILINE):
                    completion_reason = "FAIL"
                    summary = "子 Agent 认为任务无法用代码完成。"
                    break
                no_code_streak += 1
                if no_code_streak >= 3:
                    completion_reason = "FAIL"
                    summary = "连续多次未输出代码块（模型陷入无效思考），提前终止。"
                    break
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "请只输出一个 ```python / ```bash / ```note 代码块，或 DONE / FAIL。不要输出思考或解释。"})
                continue

            no_code_streak = 0
            viewed_image = None   # view 块读到的图片，下一步要展示给模型看

            if code_type == "python":
                result = self.env.run_python(code)
                history_entry = {"type": "python", "code": code[:200], "result": result[:500]}
            elif code_type == "bash":
                result = self.env.run_command(code)
                history_entry = {"type": "bash", "code": code[:200], "result": result[:500]}
            elif code_type == "view":
                viewed_image = self._read_image(code)
                if viewed_image is not None:
                    result = "图片已加载，下面会显示给你看。"
                    history_entry = {"type": "view", "code": code[:200], "result": "已加载图片"}
                else:
                    result = f"无法加载图片: {code}（确认路径正确、是 png/jpg 等图片格式）"
                    history_entry = {"type": "view", "code": code[:200], "result": "加载失败"}
            else:  # note
                key, value = _parse_note(code)
                if key:
                    self.notes[key] = value
                    history_entry = {"type": "note", "code": f"{key}={value[:50]}", "result": "已存"}
                    result = f"已记录 note: {key}"
                else:
                    history_entry = {"type": "note", "code": code[:200], "result": "未解析出 key/value"}
                    result = "note 块需含 key= 和 value="

            execution_history.append(history_entry)
            messages.append({"role": "assistant", "content": raw})
            if viewed_image is not None:
                # view 块：把图片作为下一轮的视觉输入，让子 Agent 直接看图
                messages.append(self.model.build_vision_message(
                    f"这是你请求查看的图片 {code}，请读取其中你需要的数据。",
                    [(None, viewed_image)]))
            else:
                messages.append({"role": "user", "content": f"执行结果：\n{result}\n\n继续下一步。"})

        return {
            "task_instruction": task_instruction,
            "steps_executed": len(execution_history),
            "budget": self.budget,
            "completion_reason": completion_reason,
            "summary": summary,
            "notes": dict(self.notes),
            "execution_history": execution_history,
        }
