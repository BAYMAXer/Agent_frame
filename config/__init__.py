"""配置包：所有可调项 + 动作/skill 注册表文件的定位。

- Config 系列 dataclass：模型、grounding、agent 循环、环境的可调项。
- ACTION_YAML / SKILLS_YAML：动作 / skill 注册表文件路径（随包分发，改这些 yaml 不动代码）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional


# 注册表文件路径（相对本包，随包走，换机器也能找到）
CONFIG_DIR = Path(__file__).parent
ACTION_YAML = CONFIG_DIR / "action.yaml"
SKILLS_YAML = CONFIG_DIR / "skills.yaml"
_ENV_REFERENCE = re.compile(r"^(?:env:([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\})$")


def resolve_secret(value: str) -> str:
    """Resolve ``env:NAME``/``${NAME}`` without putting credentials in tracked YAML.

    Process environment variables take precedence.  For local development the
    repository's ignored ``.env`` file is used as a small fallback, so the web
    UI and subprocess runners can share one credential without exposing it in
    command lines or model preset responses.
    """
    if not isinstance(value, str):
        raise TypeError("secret reference must be a string")
    match = _ENV_REFERENCE.fullmatch(value.strip())
    if match is None:
        return value
    name = match.group(1) or match.group(2)
    secret = os.getenv(name)
    if secret:
        return secret
    dotenv = CONFIG_DIR.parent / ".env"
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() != name:
            continue
        candidate = candidate.strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
            candidate = candidate[1:-1]
        if candidate:
            return candidate
    raise RuntimeError(f"缺少环境变量 {name}（也未在本地 .env 中配置）")


def load_yaml(path: Path) -> Dict[str, Any]:
    """读取一个 yaml 文件，返回 dict（缺 PyYAML 时给中文报错）。"""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("读取 yaml 需要 PyYAML，请 pip install pyyaml") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# 各子配置
# --------------------------------------------------------------------------- #
@dataclass
class ModelConfig:
    """决策模型的连接配置。"""
    name: str = "Qwen3.6-27B"
    url: str = "http://10.44.101.62:8094/v1"
    api_key: str = "EMPTY"
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 180.0
    vision: bool = True              # 是否把截图喂给决策模型（纯文本模型设 false）
    disable_thinking: bool = True    # 关闭 Qwen 思考模式（避免模型生成大段 chain-of-thought 陷入循环）
    capture_rollout: bool = False    # Request real server token IDs/logprobs; never reconstruct them.
    policy_revision: str = ""        # Expected immutable behavior revision, checked against server metadata.
    tokenizer_revision: str = ""
    processor_revision: str = ""
    chat_template_revision: str = ""


@dataclass
class GroundingConfig:
    """视觉定位模型的连接配置。"""
    name: str = "ui_venus2_9b"
    url: str = "http://7.246.80.189:8001/v1"
    api_key: str = "EMPTY"
    width: int = 1920
    height: int = 1080
    timeout: float = 180.0


@dataclass
class AgentConfig:
    """智能体循环的超参。"""
    max_steps: int = 40
    max_retries: int = 3
    enable_reflection: bool = True
    early_stop_repeat: int = 3
    max_replans: int = 3
    wait_config: Dict[str, Any] = field(default_factory=dict)
    progress_config: Dict[str, Any] = field(default_factory=dict)
    decision_judge: Dict[str, Any] = field(default_factory=dict)
    knowledge_method: str = "all"
    knowledge_jev_url: str = ""


@dataclass
class EnvConfig:
    """环境（OSWorld）的配置。"""
    provider: str = "osworld"
    os: str = "linux"               # 目标机操作系统：linux / mac / windows（决定终端命令的 shell）
    vm_path: str = "/data/osworld-agent-s/vm/uploaded/Ubuntu.qcow2"
    sleep_after_execution: float = 1.5
    headless: bool = False
    screen_width: int = 1920
    screen_height: int = 1080


@dataclass
class ContextConfig:
    """输入上下文预算（browser AX-Tree / 桌面 AX / 截图 / 记忆 / 工具结果的分层预算）。"""
    context_window: int = 65536
    max_input_tokens: int = 30000
    output_reserve: int = 2048
    safety_margin: int = 1024
    image_tokens: int = 2048   # 估计值，部署时按实际 processor 调整
    max_images: int = 3
    structured_max_images: int = 1
    memory_tokens: int = 2200
    auxiliary_tokens: int = 1200
    structure_tokens: int = 23000
    tokenizer_path: str = ""  # 本地 tokenizer；不填则按 UTF-8 字节保守预算
    require_current_image: bool = True


@dataclass
class Config:
    """顶层配置，聚合各子配置。"""
    model: ModelConfig = field(default_factory=ModelConfig)
    grounding_model: GroundingConfig = field(default_factory=GroundingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    completion: Optional[Dict[str, Any]] = None
    private_completion: Optional[Dict[str, Any]] = None
    planner_model: Optional[ModelConfig] = None
    event_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从嵌套 dict 构造，缺失的子块用默认值。"""
        return cls(
            model=ModelConfig(**(data.get("model") or {})),
            grounding_model=GroundingConfig(**(data.get("grounding_model") or {})),
            agent=AgentConfig(**(data.get("agent") or {})),
            env=EnvConfig(**(data.get("env") or {})),
            context=ContextConfig(**(data.get("context") or {})),
            completion=data.get("completion"),
            private_completion=data.get("private_completion"),
            planner_model=ModelConfig(**data["planner_model"]) if data.get("planner_model") else None,
            event_path=data.get("event_path"),
        )

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        """从 yaml/json 文件加载；不传则返回全默认配置。"""
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(text) or {}
            except ImportError as exc:
                raise RuntimeError("读取 yaml 需要 PyYAML，请 pip install pyyaml") from exc
        else:
            data = json.loads(text)
        return cls.from_dict(data)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
