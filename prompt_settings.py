"""Validated per-surface decision templates, shared by Studio and runners."""
import json
from pathlib import Path

MODES = frozenset({'dom', 'gui_browser', 'gui_desktop', 'cli'})


def validate_mode_prompts(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or value.keys() - MODES:
        raise ValueError('提示词必须按 dom / gui_browser / gui_desktop / cli 分别配置')
    result = {}
    for mode, text in value.items():
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'{mode} 提示词不能为空')
        if len(text) > 40000:
            raise ValueError(f'{mode} 提示词超过 40000 字符')
        result[mode] = text
    return result


def load_mode_prompts(path):
    if not path:
        return {}
    return validate_mode_prompts(json.loads(Path(path).read_text(encoding='utf-8')))


def prompt_cli_args(value, directory):
    """Snapshot UI edits before launching, without passing large text in argv."""
    import uuid
    templates = validate_mode_prompts(value)
    if not templates:
        return []
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'decision_prompts_{uuid.uuid4().hex}.json'
    path.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding='utf-8')
    return ['--mode-prompts-file', str(path.resolve())]
