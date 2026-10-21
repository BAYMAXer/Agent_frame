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

支持将 Yaero 部署在服务器 A，由电脑 B 上的其他 Agent 调用：**A 负责规划、决策、grounding 和验收，B 只负责浏览器观察与动作执行。** B 无需部署 Yaero 的推理模型。远程模式只支持浏览器页面、标签页、网页对话框和下载，不提供 B 的终端或系统桌面控制。

A 安装 `requirements-remote-agent.txt`，将 `config/mcp.remote.example.json` 复制为 `config/mcp.remote.local.json`，填入模型配置。该样例的 `default-browser` profile 使用 `execution_mode="remote_browser"`，绑定 `browser-b` 和 `http://127.0.0.1:9876`。

以下命令中的 `python` 应使用各机器已安装对应依赖的 Python 3.12 环境。A 启动 MCP：

```sh
python launch_mcp.py --config config/mcp.remote.local.json --transport streamable-http --host 127.0.0.1 --port 8765
```

B 安装 `requirements-executor.txt` 和 Chromium：

```powershell
python -m pip install -r requirements-executor.txt
python -m playwright install chromium
python launch_browser_executor.py --host 127.0.0.1 --port 8766 --device-id browser-b --token-env YAERO_BROWSER_TOKEN --workspace-root "D:\Yaero Browser Workspace"
```

启动前，在 A 与 B 的进程环境中设置同一个随机 `YAERO_BROWSER_TOKEN`（至少 16 字符）。模型服务密钥只留在 A；浏览器工作区与原始下载文件在 B，A 只接收验收所需的证据镜像。B 为 macOS/Linux 时，将工作区换成本机绝对路径。

在 B 的另一个终端建立双向 SSH 转发，将 `user@A` 替换为实际登录地址：

```sh
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 -R 9876:127.0.0.1:8766 user@A
```

B 的上层 Agent 通过 Streamable HTTP 连接 **`http://127.0.0.1:8765/mcp`**。五个工具为 `get_capabilities`、`start_task`、`get_task`、`get_result`、`cancel_task`；提交目标后按 `run_id` 查询结果。模型不能通过任务参数更换执行设备或地址。

把整个 [`integrations/skills/yaero-computer-task`](integrations/skills/yaero-computer-task/SKILL.md) 文件夹放入上层 Agent 的技能目录，保留 `references/`。MCP 注册与 Skill 加载需要分别完成。详细安装、配置和 Python 客户端示例见 [A/B MCP 接入说明](docs/mcp-integration.md)。

原有本地浏览器/桌面模式继续使用 `config/mcp.example.json` 和平台依赖；`launch_mcp.py` 默认使用 stdio，本地模式操作 MCP 所在机器。

本版本包含运行源码、界面资源、依赖清单、示例配置、接入说明及必要技能文件。测试、开发用虚拟环境、缓存、运行记录和打包清单均排除在发布范围之外。
