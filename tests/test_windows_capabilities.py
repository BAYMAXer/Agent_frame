"""Local desktop prompts and helpers use capabilities of the actual environment."""
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import Mock

from osworld_agent.agent import Agent
from osworld_agent.code_agent import CodeAgent, CODE_AGENT_PROMPT
from osworld_agent.prompts import build_system_prompt
from osworld_agent.skills import SkillContext, run_skill


def test_windows_prompt_offers_gui_save_without_unavailable_uno():
    prompt = build_system_prompt(os_name="windows", browser_only=False)
    assert "GUI 操作智能体" in prompt
    assert "set_cell_values" not in prompt
    assert "- save：" not in prompt
    assert "程序化 store" not in prompt
    assert 'keys: ["ctrl", "s"]' in prompt
    assert "verify_file" in prompt
    assert "calculate" in prompt


def test_vm_prompt_keeps_uno_and_programmatic_save():
    prompt = build_system_prompt(os_name="linux", browser_only=False)
    assert "set_cell_values" in prompt
    assert "- save：" in prompt
    assert "程序化 store" in prompt


def test_agent_filters_actions_by_environment_capabilities():
    env = SimpleNamespace(os_name="windows", browser_only=False,
                          supported_action_names=frozenset({"click", "request_finish"}))
    agent = Agent(decision_model=Mock(), grounding=Mock(), env=env)
    assert "- click：" in agent.system_prompt
    assert "- request_finish：" in agent.system_prompt
    assert "- goto：" not in agent.system_prompt
    assert "- hotkey：" not in agent.system_prompt
    assert 'keys: ["ctrl", "s"]' not in agent.system_prompt


def test_empty_action_registry_does_not_restore_default_capabilities():
    prompt = build_system_prompt(action_registry={}, skill_registry={})
    assert "- click：" not in prompt
    assert "- save：" not in prompt
    assert "- calculate：" not in prompt
    assert "用 calculate 技能" not in prompt
    assert "Name Box" not in prompt


def test_windows_code_agent_uses_cmd_and_python_instructions():
    model = Mock()
    model.chat.return_value = "DONE"
    agent = CodeAgent(model, SimpleNamespace(os_name="windows"))
    result = agent.execute("Check a local document")
    prompt = model.chat.call_args.args[0][0]["content"]
    assert result["completion_reason"] == "DONE"
    assert "Windows cmd" in prompt
    assert "cmd /c" in prompt
    assert "Python 标准库" in prompt
    for unsupported in ("sudo", "/home/user", "用 cat", "unzip -p"):
        assert unsupported not in prompt


def test_vm_code_agent_preserves_target_machine_prompt():
    model = Mock()
    model.chat.return_value = "DONE"
    CodeAgent(model, SimpleNamespace(os_name="linux")).execute("Check a VM document")
    assert model.chat.call_args.args[0][0]["content"] == CODE_AGENT_PROMPT


def test_code_agent_reads_local_image_only_for_explicit_local_environment(tmp_path):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"test image bytes")
    local_agent = CodeAgent(Mock(), SimpleNamespace(local_environment=True))
    assert local_agent._read_image(str(path)) == b"test image bytes"
    assert local_agent._read_image(str(tmp_path / "missing.png")) is None
    for remote_env in (SimpleNamespace(os_name="windows"),
                       SimpleNamespace(os_name="windows", local_environment=False)):
        assert CodeAgent(Mock(), remote_env)._read_image(str(path)) is None


def test_verify_file_checks_local_target_using_environment_runner(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("saved", encoding="utf-8")

    def run_python(script):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(script, {})
        return output.getvalue()

    runner = Mock(side_effect=run_python)
    env = SimpleNamespace(local_environment=True, run_python=runner)
    result = run_skill("verify_file", {"path": str(path)}, SkillContext(env))
    assert result.startswith("FILE exists: ")
    assert str(path) in result
    assert "5 bytes" in result
    missing = run_skill("verify_file", {"path": str(tmp_path / "missing.txt")}, SkillContext(env))
    assert missing.startswith("NOT FOUND: ")
    assert runner.call_count == 2


def test_verify_file_does_not_read_host_files_when_remote_controller_missing(tmp_path):
    path = tmp_path / "host-only.txt"
    path.write_text("host file", encoding="utf-8")
    runner = Mock()
    env = SimpleNamespace(os_name="windows", run_python=runner)
    result = run_skill("verify_file", {"path": str(path)}, SkillContext(env))
    assert "无法执行" in result
    runner.assert_not_called()
