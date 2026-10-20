# 文鳐 Yaero · Agent_frame

**v1.0.0** · 支持 GUI、DOM 与 CLI 的智能体框架，提供任务规划、执行、逐步核查、Studio 工作台和 MCP 接入。

Python 3.12。源码位于仓库根目录，`launch.py` 支持任意克隆目录名。

## 安装与启动

Windows（PowerShell）：

```powershell
git clone https://github.com/BAYMAXer/Agent_frame.git
cd Agent_frame
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt -r requirements-viz.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe launch.py osworld_agent.viz.server
```

macOS：

```sh
git clone https://github.com/BAYMAXer/Agent_frame.git
cd Agent_frame
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-mac.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python launch.py osworld_agent.viz.server
```

打开 <http://127.0.0.1:8088>。通过环境变量 `AGENTS_VIZ_PORT` 可更改端口。

## 配置

在 Studio 中配置可访问的决策、定位和核查模型。`config/model_presets.yaml` 与 `config.yaml` 中的端点为示例，需要按实际部署修改。

密钥字段可使用 `env:YOUR_API_KEY` 引用环境变量，也可从本地 `.env` 读取同名变量。`.env.example` 提供可选组件的配置示例。本地凭据和个人设置已由 `.gitignore` 排除。

## MCP 接入

在上述环境中安装并检查入口：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
Copy-Item config\mcp.example.json config\mcp.local.json
.\.venv\Scripts\python.exe launch_mcp.py --help
```

编辑 `config/mcp.local.json` 配置模型与执行环境。MCP 宿主使用 Python 解释器和 `launch_mcp.py` 的绝对路径，并传入 `--config` 与本地配置文件路径；默认使用 stdio。

配套技能位于 [`integrations/skills/yaero-computer-task`](integrations/skills/yaero-computer-task/SKILL.md)。技能说明与运行必需的参考文件随源码保留。

本版本仅包含运行源码、界面资源、依赖清单、示例配置及必要技能文件。测试、开发文档、虚拟环境、缓存、运行记录和打包清单均排除在发布范围之外。
