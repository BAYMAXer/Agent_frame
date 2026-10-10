"""评测层：批量/并行 runner + 计分报告（占位）。

下一步接入 OSWorld 后端 + 分片清单后，实现：
- run_tasks：批量跑任务（支持并行分片，参考服务器上已验证的 osworld_parallel.sh 思路）
- report：汇总通过率 + 中文报告
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..agent import AgentResult


@dataclass
class EvalSummary:
    total: int = 0
    passed: int = 0
    results: List[AgentResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0


def run_tasks(agent_factory, tasks: List[dict], parallel: int = 1) -> EvalSummary:
    """批量跑任务。agent_factory: (task) -> Agent。

    TODO：parallel > 1 时用多进程分片（每个 worker 独立端口组），
    复用服务器上已验证的确定性端口方案。
    """
    summary = EvalSummary()
    for t in tasks:
        agent = agent_factory(t)
        result = agent.run(t["instruction"])
        summary.total += 1
        if result.score >= 1.0:
            summary.passed += 1
        summary.results.append(result)
    return summary


def render_report(summary: EvalSummary) -> str:
    """生成中文报告。"""
    lines = [
        "# 中文整体准确率报告",
        "",
        f"- 总任务数：{summary.total}",
        f"- 通过数：{summary.passed}",
        f"- 通过率：{summary.accuracy * 100:.2f}%",
        "",
        "## 逐题结果",
    ]
    for r in summary.results:
        mark = "通过" if r.score >= 1.0 else "未通过"
        lines.append(f"- {r.task}: {mark}（{r.steps} 步，答案 {r.final_answer!r}）")
    return "\n".join(lines)
