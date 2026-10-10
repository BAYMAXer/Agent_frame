"""viz 可视化：前端启动 / 实时看过程 / 看历史（自包含，不改框架文件）。

用法：
1. 设置环境变量（或直接跑，有默认值）：
   - OSWORLD_EXAMPLES_DIR     = evaluation_examples/examples 目录
   - OSWORLD_DESKTOP_ENV_PATH = 包含 desktop_env/ 的目录（如 D:/400-project/AgentS）
   - OSWORLD_VM_PATH          = VMware .vmx 路径
   - AGENTS_RESULTS_DIR       = 历史轨迹落盘目录（默认 viz/runs）
2. 启动 server：python -m osworld_agent.viz.server
3. 浏览器打开 http://127.0.0.1:8088
"""
