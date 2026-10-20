"""智能体：组件持有者 + 对外入口。

真正的「观察 → 决策 → 定位 → 执行 → 记忆更新」主循环在 pipeline.py 的 Pipeline 里，
这里只负责：持有各组件（决策模型 / grounding / 环境）+ 构造 system prompt +
把 run 委托给 Pipeline。

想改整体流程（加阶段、改早停、改记忆）→ 去 pipeline.py 的 Pipeline.run()。
"""
from __future__ import annotations

from .env import Environment
from .model import ChatModel, Grounding
from .pipeline import Pipeline, AgentResult
from .prompts import build_system_prompt


class Agent:
    """GUI 操作智能体（对外入口）。"""

    def __init__(self, decision_model: ChatModel, grounding: Grounding,
                 env: Environment, max_steps: int = 40, max_retries: int = 5,
                 enable_reflection: bool = True, early_stop_repeat: int = 3,
                 code_agent_budget: int = 20, step_callback=None,
                 completion_spec=None, context_config=None, private_completion_spec=None,
                 planner_model=None, event_path=None, run_manifest=None, max_replans=3,
                 wait_config=None, verifier_model=None, progress_config=None, decision_judge=None, mode_prompts=None, knowledge_method="all", knowledge_jev_url=""):
        from .prompt_settings import validate_mode_prompts
        self.mode_prompts = validate_mode_prompts(mode_prompts)
        from .knowledge_selection import validate_method
        self.knowledge_method = validate_method(knowledge_method)
        self.knowledge_jev_url = knowledge_jev_url
        self.model = decision_model
        self.grounding = grounding
        self.env = env
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.enable_reflection = enable_reflection
        self.early_stop_repeat = early_stop_repeat
        self.max_replans = max_replans
        self.wait_config = wait_config
        self.progress_config = progress_config
        self.decision_judge = decision_judge
        self.verifier_model = verifier_model
        self.step_callback = step_callback
        self.completion_spec = completion_spec
        self.context_config = context_config
        self.private_completion_spec = private_completion_spec
        self.planner_model = planner_model or decision_model
        self.event_path = event_path
        self.run_manifest = run_manifest
        self.os_name = getattr(env, "os_name", "linux")
        # 浏览器专用环境（WindowsEnvironment）用精简提示词：去掉表格/子Agent/技能，强化 DOM 定位
        self.browser_only = bool(getattr(env, "browser_only", False))
        self.system_prompt = build_system_prompt(os_name=self.os_name, browser_only=self.browser_only)
        self.code_agent = None

    def run(self, task: str, completion_spec=None, *, private_completion_spec=None,
            max_steps=None, event_path=None, run_id=None) -> AgentResult:
        """执行一个任务，返回结果。主循环在 pipeline.Pipeline 里。"""
        budget = self.max_steps if max_steps is None else max_steps
        if type(budget) is not int or budget < 1:
            raise ValueError("max_steps must be a positive integer")
        pipeline = Pipeline(
            model=self.model,
            grounding=self.grounding,
            env=self.env,
            system_prompt=self.system_prompt,
            mode_prompts=self.mode_prompts,
            knowledge_method=self.knowledge_method, knowledge_jev_url=self.knowledge_jev_url,
            code_agent=self.code_agent,
            max_steps=budget,
            max_retries=self.max_retries,
            enable_reflection=self.enable_reflection,
            early_stop_repeat=self.early_stop_repeat,
            max_replans=self.max_replans,
            wait_config=self.wait_config,
            progress_config=self.progress_config,
            decision_judge=self.decision_judge,
            step_callback=self.step_callback,
            completion_spec=self.completion_spec if completion_spec is None else completion_spec,
            context_config=self.context_config,
            private_completion_spec=self.private_completion_spec if private_completion_spec is None else private_completion_spec,
            planner_model=self.planner_model,
            verifier_model=self.verifier_model,
            event_path=self.event_path if event_path is None else event_path,
            run_id=run_id, run_manifest=self.run_manifest,
        )
        try:
            return pipeline.run(task)
        finally:
            if pipeline.decision_judge is not None:
                pipeline.decision_judge.close()
