"""Docker 环境适配器：用 OSWorld 的 Docker provider 跑任务（服务器/无桌面环境用）。

与 VMwareEnvironment 逻辑一致，只是 provider_name 换成 docker。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .vmware_env import VMwareEnvironment


class DockerEnvironment(VMwareEnvironment):
    def __init__(self, task_config: Dict[str, Any], vm_path: Optional[str] = None,
                 headless: bool = True, screen_width: int = 1920,
                 screen_height: int = 1080, snapshot_name: str = "init_state_ca3",
                 desktop_env_path: Optional[str] = None, cache_dir: Optional[str] = None):
        super().__init__(
            task_config=task_config, vm_path=vm_path, headless=headless,
            screen_width=screen_width, screen_height=screen_height,
            snapshot_name=snapshot_name, desktop_env_path=desktop_env_path,
            provider="docker", cache_dir=cache_dir,
        )
