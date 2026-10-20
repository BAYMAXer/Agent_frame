"""Local background-only environment; does not import desktop input libraries."""
import os
import sys
import time
from pathlib import Path

from ..env import Environment, Observation, StepResult
from ..cli_executor import cli_capability, execute_cli
from ..workspace_config import default_workspace_root


class CLIEnvironment(Environment):
    cli_target = 'local'
    cli_only = True
    browser_only = False
    evaluation_available = False

    def __init__(self, downloads_dir=None, workspace_dir=None, **_):
        self.os_name = 'windows' if sys.platform == 'win32' else 'mac' if sys.platform == 'darwin' else 'linux'
        self.workspace_root = str(Path(workspace_dir or downloads_dir or default_workspace_root()).expanduser().absolute())
        self.workspace_dir = os.path.join(self.workspace_root, time.strftime('%Y-%m-%d'))
        self.initial_state = {}

    def prepare_workspace(self, task_id=None):
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        self.initial_state = {'workspace_dir': self.workspace_dir, 'workspace_root': self.workspace_root,
                              'workspace_location': 'target_desktop', 'workspace_scope': 'day', 'cli_only': True}
        self.initial_state['cli_capability'] = cli_capability(self)
        return dict(self.initial_state)

    def reset(self, task):
        if not self.initial_state:
            self.prepare_workspace()
        return self.observe()

    def observe(self):
        return Observation(info={'active_surface':'terminal', 'cli_capability':cli_capability(self),
                                 'initial_state':dict(self.initial_state)})

    def step(self, action):
        if action.action == 'wait':
            time.sleep(min(60, max(0, float(action.args.get('seconds', 1)))))
            return StepResult(self.observe(), info={'status':'executed','channel':'cli','action':'wait'})
        return StepResult(self.observe(), info={'status':'rejected','dispatched':False,'channel':'cli',
                                               'reason':'仅后台 CLI 环境不提供浏览器或桌面交互'})

    def run_command(self, command):
        import json
        return json.dumps(execute_cli(self, {'operation':'shell','command':command}), ensure_ascii=False)

    def run_python(self, code):
        raise NotImplementedError('Use the registered CLI action with an explicit command')

    def get_page_source(self):
        return ''

    def evaluate(self):
        return 0.0

    def close(self):
        pass
