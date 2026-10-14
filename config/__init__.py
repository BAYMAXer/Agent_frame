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
DEFAULT_AX_MAX_CHARS = 12000


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
    name: str = "Qwen3.5-9B"
    url: str = "http://7.246.80.189:9028/v1"
    api_key: str = "EMPTY"
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 60.0
    request_retries: int = 0         # SDK 隐式重试关闭；显式重试也受该上限约束
    vision: bool = True              # 是否把截图喂给决策模型（纯文本模型设 false）
    disable_thinking: bool = True    # 关闭 Qwen 思考模式（避免模型生成大段 chain-of-thought 陷入循环）


@dataclass
class GroundingConfig:
    """视觉定位模型的连接配置。"""
    name: str = "UI-Venus-2-9B"
    url: str = "http://7.246.80.189:8001/v1"
    api_key: str = "EMPTY"
    width: int = 1920
    height: int = 1080
    timeout: float = 60.0
    type: str = "ui-venus2"


@dataclass
class AgentConfig:
    """智能体循环的超参。"""
    max_steps: int = 20
    max_retries: int = 3
    enable_reflection: bool = True
    early_stop_repeat: int = 3
    step_timeout: float = 120.0


@dataclass
class EnvConfig:
    """本地 Windows 环境；用户可选普通或无痕，默认独立无痕实例。"""
    screen_width: int = 1920
    screen_height: int = 1080
    execution_mode: str = "browser_first"
    browser_mode: str = "incognito"
    browser_port: int = 9222
    chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    downloads_dir: Optional[str] = None
    action_settle_seconds: float = 0.3

    def __post_init__(self):
        if not isinstance(self.execution_mode, str) or self.execution_mode not in {"desktop", "browser_first"}:
            raise ValueError("execution_mode 必须是 desktop 或 browser_first")
        if not isinstance(self.browser_mode, str) or self.browser_mode not in {"incognito", "normal"}:
            raise ValueError("browser_mode 必须是 incognito 或 normal")


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
    ax_max_chars: int = DEFAULT_AX_MAX_CHARS  # 浏览器精简 AX 的字符上限，与 structure_tokens 的 token 预算独立
    screenshot_max_edge: int = 1280  # 浏览器模型输入截图最长边；保留原始坐标用于操作
    tokenizer_path: str = ""  # 本地 tokenizer；不填则按 UTF-8 字节保守预算

    def __post_init__(self):
        for name in ("ax_max_chars", "screenshot_max_edge"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")


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
