"""配置包：所有可调项 + 动作/skill 注册表文件的定位。

- Config 系列 dataclass：模型、grounding、agent 循环、环境的可调项。
- ACTION_YAML / SKILLS_YAML：动作 / skill 注册表文件路径（随包分发，改这些 yaml 不动代码）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional


# 注册表文件路径（相对本包，随包走，换机器也能找到）
CONFIG_DIR = Path(__file__).parent
ACTION_YAML = CONFIG_DIR / "action.yaml"
SKILLS_YAML = CONFIG_DIR / "skills.yaml"


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
    name: str = "9b"
    url: str = "http://7.246.80.237:9028/v1"
    api_key: str = "EMPTY"
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 180.0
    vision: bool = True              # 是否把截图喂给决策模型（纯文本模型设 false）
    disable_thinking: bool = True    # 关闭 Qwen 思考模式（避免模型生成大段 chain-of-thought 陷入循环）


@dataclass
class GroundingConfig:
    """视觉定位模型的连接配置。"""
    name: str = "ui_tars_7b"
    url: str = "http://7.246.80.237:49999/v1"
    api_key: str = "EMPTY"
    width: int = 1920
    height: int = 1080
    timeout: float = 180.0


@dataclass
class AgentConfig:
    """智能体循环的超参。"""
    max_steps: int = 20
    max_retries: int = 3
    enable_reflection: bool = True
    early_stop_repeat: int = 3


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
    structure_tokens: int = 5000
    tokenizer_path: str = ""  # 本地 tokenizer；不填则按 UTF-8 字节保守预算


@dataclass
class Config:
    """顶层配置，聚合各子配置。"""
    model: ModelConfig = field(default_factory=ModelConfig)
    grounding_model: GroundingConfig = field(default_factory=GroundingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    context: ContextConfig = field(default_factory=ContextConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从嵌套 dict 构造，缺失的子块用默认值。"""
        return cls(
            model=ModelConfig(**(data.get("model") or {})),
            grounding_model=GroundingConfig(**(data.get("grounding_model") or {})),
            agent=AgentConfig(**(data.get("agent") or {})),
            env=EnvConfig(**(data.get("env") or {})),
            context=ContextConfig(**(data.get("context") or {})),
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
