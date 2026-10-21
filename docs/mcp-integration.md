# A 负责推理、B 执行浏览器的 MCP 接入

此模式让另一台电脑 B 上的 Agent 调用服务器 A 的 Yaero。规划、决策、grounding 定位和验收都在 A 的框架中进行；B 只运行轻量浏览器执行器，返回浏览器截图和可访问性结构，并执行经过校验的浏览器动作。

| 位置 | 运行内容 | 保存内容 |
| --- | --- | --- |
| A：Yaero 服务器 | MCP、任务管理、规划、决策、grounding、验收 | 模型配置和密钥、运行记录、下载证据镜像 |
| B：用户电脑 | 上层 Agent、浏览器执行器、专用 Chromium | 浏览器登录状态、原始下载文件 |

上层 Agent 委派完整目标，并按 `run_id` 跟踪。B 不需要配置 Yaero 的模型服务，也不运行另一套规划循环。当前远程执行范围是 `browser_only`，不包括 B 的桌面软件、终端或系统文件对话框。

```mermaid
flowchart LR
    U["B：上层 Agent + Skill"] -->|"MCP / 任务目标"| A["A：规划、决策、定位、验收"]
    A -->|"浏览器动作"| E["B：轻量浏览器执行器"]
    E -->|"截图、AX、下载证据"| A
    A -->|"状态、结果"| U
```

## 1. 准备 A 和 B

两台机器使用同一版本源码及 Python 3.12。B 需要可运行 Chromium；A 在远程模式下不需要本机浏览器、桌面权限或 Playwright。下面的端口分别是 A 的 MCP `8765`、B 的执行器 `8766`、A 上转发到 B 的入口 `9876`。

在 A 的源码目录安装远程 Agent 依赖。Linux/macOS：

```sh
python3.12 -m venv .venv-agent
.venv-agent/bin/python -m pip install -r requirements-remote-agent.txt
cp config/mcp.remote.example.json config/mcp.remote.local.json
```

A 为 Windows 时，在 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv-agent
& ".\.venv-agent\Scripts\python.exe" -m pip install -r requirements-remote-agent.txt
Copy-Item -LiteralPath ".\config\mcp.remote.example.json" -Destination ".\config\mcp.remote.local.json"
```

在 B 的源码目录安装执行器依赖。Windows PowerShell：

```powershell
py -3.12 -m venv .venv-executor
& ".\.venv-executor\Scripts\python.exe" -m pip install -r requirements-executor.txt
& ".\.venv-executor\Scripts\python.exe" -m playwright install chromium
```

B 为 macOS/Linux 时，将虚拟环境命令换成 `python3.12 -m venv .venv-executor`，解释器路径换成 `.venv-executor/bin/python`。B 的依赖不包含 MCP、OpenAI SDK 或桌面鼠标键盘控制库；如果 B 的上层 Agent 自己使用 MCP SDK，另在它自己的环境安装。

Linux B 还需要 Chromium 的系统依赖。首次部署可用下面命令同时安装浏览器和系统依赖；安装系统包需要相应系统权限：

```sh
.venv-executor/bin/python -m playwright install --with-deps chromium
```

以上复制配置命令仅在首次配置时执行，避免覆盖已有的本机配置。A 与 B 的路径可以完全不同，所有命令都从各自源码目录运行。

## 2. 配置 A：模型与固定的 B 设备

编辑 A 上的 [`config/mcp.remote.local.json`](../config/mcp.remote.example.json)，以样例为准填写模型名称与接口地址。关键 profile 字段如下：

| 字段 | 所属位置与含义 |
| --- | --- |
| `model`、`model_url`、`model_api_key` | A 使用的决策模型；密钥可用 `env:YAERO_MODEL_API_KEY` 引用 |
| `ground_model`、`ground_type`、`ground_url`、`ground_api_key` | A 使用的 grounding 模型；密钥可用 `env:YAERO_GROUND_API_KEY` 引用 |
| `execution_mode` | 设置为 `remote_browser` |
| `browser_executor.endpoint` | A 访问 B 执行器的地址，本例为 `http://127.0.0.1:9876` |
| `browser_executor.device_id` | 与 B 启动参数一致，本例为 `browser-b` |
| `browser_executor.token_env` | 共享执行器令牌的环境变量名，本例为 `YAERO_BROWSER_TOKEN` |

生成一个至少 16 字符的随机共享令牌，分别放入 A 的 MCP/运行器进程环境和 B 的执行器进程环境中的 `YAERO_BROWSER_TOKEN`。两端的值必须相同，不能把模型 API 密钥当作此令牌。执行器令牌必须在实际进程环境中，不能只写入 `.env`；模型服务凭据只配置在 A，上层任务参数不传密钥。B 缺少令牌或令牌过短时会拒绝启动。

A 的顶层 `runs_root` 控制运行记录目录，`runner_python` 可指定执行任务的 Python。配置中的相对路径相对于配置文件所在目录解析。B 的下载工作区、浏览器 profile 和登录状态由 B 的启动参数配置，不要把 B 的磁盘路径写成 A 的工作目录。

一个 profile 固定绑定一个执行器地址和 `device_id`。上层 Agent 只能选择服务端公布的 profile，不能在 `start_task` 中临时指定执行器地址。A 的入口会按 profile 自动选择远程运行器。

## 3. 在 B 启动浏览器执行器

先在启动进程的环境中配置 `YAERO_BROWSER_TOKEN`，然后执行：

```powershell
& ".\.venv-executor\Scripts\python.exe" ".\launch_browser_executor.py" `
  --host 127.0.0.1 --port 8766 `
  --device-id browser-b --token-env YAERO_BROWSER_TOKEN `
  --workspace-root "D:\Yaero Browser Workspace"
```

在 macOS/Linux 上，使用 `.venv-executor/bin/python launch_browser_executor.py` 和本机绝对工作区路径，例如 `/home/user/yaero-browser-workspace`。

默认启动执行器专用的 Chromium 和持久 profile。可通过 `--profile-dir` 指定 B 上的专用 profile，或通过 `--cdp-url http://127.0.0.1:9222` 连接已准备好的专用浏览器；两种方式不要同时使用。需要登录的网站，应在这个浏览器中完成登录，不会自动继承普通浏览器的登录状态。`--headless` 可用于无需可见窗口的浏览器流程。

若 Windows 上启动完整 Chromium 时出现错误 `14001`，可在无需可见窗口或交互登录的流程中加 `--headless`，或由操作者准备启用 CDP 的专用 Chrome，再用 `--cdp-url` 连接。本次测试机器曾遇到该错误，因此这里只验证了无界面 Chromium；有界面浏览器与登录流程仍需在 B 上检查。

B 只执行浏览器页面内的动作、标签页操作、网页对话框和浏览器下载。它不提供终端、任意代码执行、系统桌面操作或任意文件读写接口。浏览器下载的原文件留在 B 的工作区。

## 4. 在 A 启动 MCP，在 B 建立两条转发

A 为 Linux/macOS：

```sh
.venv-agent/bin/python launch_mcp.py --config config/mcp.remote.local.json --transport streamable-http --host 127.0.0.1 --port 8765
```

A 为 Windows：

```powershell
& ".\.venv-agent\Scripts\python.exe" ".\launch_mcp.py" --config ".\config\mcp.remote.local.json" --transport streamable-http --host 127.0.0.1 --port 8765
```

在 B 的另一个终端启动 SSH 转发，将 `user@A` 替换为可以登录 A 的 SSH 用户和地址：

```sh
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 -R 9876:127.0.0.1:8766 user@A
```

这一个 SSH 连接承载两个方向：

| 访问地址 | 转发去向 | 使用者 |
| --- | --- | --- |
| B 的 `127.0.0.1:8765` | A 的 `127.0.0.1:8765` | B 的上层 Agent 调用 MCP |
| A 的 `127.0.0.1:9876` | B 的 `127.0.0.1:8766` | A 控制 B 的浏览器执行器 |

A 需要允许这两种 SSH TCP 转发，端口也必须空闲。保持 A 的 MCP、B 的执行器和 SSH 连接运行。两个服务都绑定回环地址；本版没有为 MCP 提供公网认证或多租户授权，不应把它直接暴露到公网。

## 5. B 上的 Agent 接入 MCP 和 Skill

在 B 的 Agent 宿主中新增 Streamable HTTP MCP，URL 为 `http://127.0.0.1:8765/mcp`。采用 `mcpServers` 格式的宿主可参考：

```json
{
  "mcpServers": {
    "yaero": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

具体配置文件位置、传输类型字段和外层结构依宿主而定；这里的 URL 访问的是 SSH 转发，不是在 B 上启动第二套 Yaero。

将整个 [`integrations/skills/yaero-computer-task`](../integrations/skills/yaero-computer-task/SKILL.md) 文件夹复制到 B 上层 Agent 的技能目录，保留 `references/`。MCP 注册提供工具，Skill 指导任务委派、重试和验收，两者需要分别完成。根目录的 `skills/` 是框架内部技能系统，不是这份外部 Skill 的安装目录。

如果自研 Agent 没有技能加载器，在选择 Yaero 的任务中加载 `SKILL.md` 正文及按需引用的参考资料，并向模型提供实际发现的工具 schema。宿主可能给工具名加前缀，以宿主实际名称为准。

## 6. 五个工具和任务生命周期

| 工具 | 作用 |
| --- | --- |
| `get_capabilities()` | 查询 profile、设备、执行范围和配置状态 |
| `start_task(...)` | 提交完整目标，返回 `run_id` |
| `get_task(run_id)` | 查询指定任务的状态和进度 |
| `get_result(run_id)` | 获取验收、回答、产物和诊断路径 |
| `cancel_task(run_id)` | 请求停止指定任务 |

`start_task` 接收 `task`、`request_id`，以及可选的 `profile`、`max_steps`、`timeout_s`、`completion_spec`。默认 profile 为 `default-browser`，预算为 40 步和 600 秒，允许范围分别为 1–200 步和 5–3600 秒。`request_id` 最多 128 字符，以 ASCII 字母或数字开头，其余只允许字母、数字或 `_.:-`；可使用 UUID。

先检查 `profiles[].execution_mode`、`device_id`、`browser_only`，确认 profile 指向用户需要的 B 设备且能力范围能完成任务。`configured=true` 是静态配置检查，`executor_connectivity_checked=false` 表示尚未检查执行器连通性；profile 的 `availability` 只反映 A 的调度状态，不能据此认定 B 已在线。对于需要 B 的桌面软件、系统另存为窗口或终端的任务，不能把浏览器模式当作通用远程桌面。

提交后保存 `request_id` 和 `run_id`，按 `poll_after_s` 轮询。`starting`、`running` 表示仍在执行；`completed`、`failed`、`cancelled`、`timed_out`、`interrupted` 为终态。等待下载、等待页面稳定和清理都属于运行过程，上层不要同时控制 B 的同一浏览器。

每次调用均检查 MCP 的 `isError` 和业务结果的 `ok`。业务错误形如 `{"ok":false,"error":{"code":"desktop_busy","message":"..."}}`。同一提交重试使用相同 `request_id` 和参数；`request_conflict` 表示该 ID 已对应不同参数。取得 `run_id` 后，即使网络超时也先查询原任务，不立即创建新任务。执行器断线或动作结果不确定时，先核对原运行和页面状态，避免重复外部操作。

`completion_spec` 默认省略。确需显式条件时，按 [`completion-spec.md`](../integrations/skills/yaero-computer-task/references/completion-spec.md) 提供已支持字段，并确保覆盖用户的完整要求。任务受理、进程退出、模型自述和文件出现均不能代替最终验收。

进入终态后读 `get_result` 的 `success`、`termination_reason`、`verification`、`final_answer` 和 `artifacts`。区分确定性验收与模型判断；失败或证据不足时说明已完成部分和剩余要求。用户要求停止时调用 `cancel_task`，再查询确认终态；取消不能撤销已完成的提交或下载。

## 7. 原文件、证据镜像和诊断

B 的浏览器保存原始下载。A 为验收取得的文件是从 B 已登记下载产物传输的证据镜像；它不是 A 在后台另行下载的替代成果。远程下载项通过 `device_id`、`remote_path` 标明 B 的原件，通过 `local_mirror_path` 和 `path_scope="controller_evidence_mirror"` 标明 A 的证据文件。不能把 A 的本地路径当作用户 B 上的文件路径。

`artifacts` 是可用产物清单，`verification` 是验收记录。任务结果中的 `execution_device_id` 指明执行设备，`planning_location` 指明推理位置；产物中的 `path`/`uri` 指向 A 可访问的文件，应结合 `remote_path` 和镜像字段理解。`status_at_run_end`、`sha256_at_run_end` 是任务结束时的证据；后续查询清单不会重新计算哈希。不能把历史验收说成已经再次读取当前文件。

五个 MCP 工具不直接把文件内容交给上层 Agent。A 与 B 之间为验收传输证据，不等于已向最终用户交付文件。上层应使用自己在 B 的文件访问能力读取已确认的原件，或使用明确的传输机制取得所需产物。

`get_result.diagnostics` 给出 A 上的 `stdout_log`、`stderr_log`、`supervisor_log` 路径，不直接返回日志正文。启动失败、缺依赖或模型连接错误先看 A 的日志；B 执行器无法连接时同时检查 B 进程、SSH 转发、`device_id` 和两端令牌是否一致。

## 8. 自研 Python Agent：从 B 连接

客户端环境安装 `mcp>=1.26,<2`。下面示例只连接和查询能力，不会执行浏览器动作：

```python
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def unpack(result):
    if result.isError:
        raise RuntimeError("\n".join(getattr(x, "text", "") for x in result.content))
    payload = result.structuredContent
    if payload is None:
        payload = json.loads(next(x.text for x in result.content if x.type == "text"))
    if not payload.get("ok", False):
        raise RuntimeError(payload.get("error", payload))
    return payload


async def main():
    async with streamable_http_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([tool.name for tool in tools.tools])
            capabilities = unpack(await session.call_tool("get_capabilities", {}))
            print(json.dumps(capabilities, ensure_ascii=False, indent=2))


asyncio.run(main())
```

在同一异步会话内加入下面片段可执行首次小任务。实际系统应把请求 ID 和运行 ID 保存到持久存储，连接恢复后继续查询原任务：

```python
import uuid

request_id = str(uuid.uuid4())
job = unpack(await session.call_tool("start_task", {
    "task": "在指定的 B 浏览器打开 https://example.com/，确认页面完整文字 Example Domain",
    "request_id": request_id,
    "profile": "default-browser",
    "max_steps": 10,
    "timeout_s": 120,
}))
run_id = job["run_id"]
terminal = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
while True:
    status = unpack(await session.call_tool("get_task", {"run_id": run_id}))
    if status["status"] in terminal:
        break
    await asyncio.sleep(status.get("poll_after_s", 3))
result = unpack(await session.call_tool("get_result", {"run_id": run_id}))
print(json.dumps(result, ensure_ascii=False, indent=2))
```

先验证 MCP 能力发现，再验证 B 浏览器观察，最后执行明确的小任务。

## 已验证范围与复测

端到端测试已在当前 Windows 机器上通过：用独立进程模拟 A、B，经 HTTP MCP 调用真实 A 侧 Agent，规划和决策使用确定性 OpenAI 兼容 HTTP 测试服务，B 使用真实无界面 Chromium。测试覆盖浏览器导航、DOM 操作、URL/可访问性结构验收和运行轨迹；下载测试覆盖真实浏览器下载及 A 侧按哈希校验的证据镜像。导入保护检查了 A 不加载本机浏览器/桌面环境，B 不加载模型运行时。

这不是两台实体机器的联调：真实 SSH 转发、实际模型服务、用户登录状态和具体业务网站尚未验证。配置样例保留模型占位值，没有为你填入真实模型密钥；部署时仍需配置实际端点、凭据及两端相同的执行器令牌。

在包含测试文件的源码工作区，可在专用测试环境复测。以下命令中的 `python` 应指向该环境：

```sh
python -m pip install -r requirements-test.txt -r requirements-remote-agent.txt -r requirements-executor.txt
python -m playwright install chromium
python -m pytest tests
```

Linux 测试机同样需要上述 Chromium 系统依赖。若使用不包含 `tests/` 的精简发布包，需要取得对应版本的完整源码工作区后运行测试。

## 本地运行兼容方式

已有的本地浏览器/桌面模式仍使用 [`config/mcp.example.json`](../config/mcp.example.json) 配置和平台依赖；它操作的是 MCP 所在机器。默认 stdio 启动命令为 `python /absolute/path/launch_mcp.py --config /absolute/path/config/mcp.local.json`，由本机 MCP 宿主启动。不要把这个模式与上述 B 浏览器执行器混用。

当前版本的任务记录在 A 持久保存。MCP 连接断开不会自行取消已启动任务；重新连接同一记录目录后查询原 `run_id`。若监督进程或远程会话也已丢失，先核对状态，不能假设原动作可安全重放。
