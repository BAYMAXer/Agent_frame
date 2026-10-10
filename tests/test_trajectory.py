"""trajectory 落盘单元测试：trainable 判定 + 图片外置去重（无需真实运行）。"""
import base64
from io import BytesIO

from PIL import Image

from osworld_agent.pipeline import AgentResult
from osworld_agent.trajectory import build_trajectory, dump_trajectory


def _data_image():
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _entry(policy_input, screenshot):
    return {
        "step": 1, "action_id": "a_1",
        "action": {"action": "click", "args": {"target": "x"}},
        "policy_input": policy_input,
        "policy_output": '{"next_action": {"type": "click", "target": "x"}}',
        "valid_action": True,
        "decision_calls": [],
        "observation": {"id": "o_1", "context": [], "screenshot": screenshot},
    }


def test_trainable_requires_policy_input_and_output():
    result = AgentResult(task="t")
    result.trajectory.append(_entry(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], None))
    traj = build_trajectory("id", "windows", "instr", result)
    step = traj["steps"][0]
    assert step["trainable"] is True
    assert step["policy_input"][0]["content"] == "S"
    assert step["policy_output"].startswith('{"next_action"')


def test_not_trainable_without_policy():
    result = AgentResult(task="t")
    result.trajectory.append({"step": 1, "action_id": "a_1",
                              "action": {"action": "click", "args": {}}, "valid_action": True})
    traj = build_trajectory("id", "windows", "instr", result)
    assert traj["steps"][0]["trainable"] is False


def test_dump_externalizes_images(tmp_path):
    result = AgentResult(task="t")
    result.trajectory.append(_entry(
        [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": _data_image()}},
        ]}],
        _data_image()))
    traj = build_trajectory("id", "windows", "instr", result)
    out = tmp_path / "trajectory.json"
    dump_trajectory(str(out), traj)
    raw = out.read_text(encoding="utf-8")
    assert "data:image/" not in raw          # 图片全部外置，无残留 base64
    assert (tmp_path / "assets").is_dir()
    assert len(list((tmp_path / "assets").iterdir())) >= 1
    assert "assets/" in raw                  # 引用的是 assets/ 相对路径
