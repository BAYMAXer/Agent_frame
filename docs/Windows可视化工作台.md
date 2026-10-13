# Windows 可视化工作台

本版本优先保障 browser use：先分解任务，再依次执行子任务；网页子任务直接进入 Agent 的独立浏览器，通过浏览器截图选择感兴趣的网页区域，再用该区域的 AX 节点定位并操作。界面提供任务配置、实时运行、历史轨迹与模型对话。通用 Windows 桌面流程作为兼容模式保留。

## 安装与启动

推荐 Python 3.12 和已安装的 Google Chrome。从项目根目录安装本机运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\osworld_agent_1010\requirements-dev.txt
.\.venv\Scripts\python.exe .\osworld_agent_1010\launch.py
```

`requirements.txt` 固定本机运行所需的直接依赖版本；`requirements-dev.txt` 额外安装 pytest。浏览器通过 CDP 连接系统 Chrome，无需为本机任务下载 Playwright 浏览器。框架只执行本地 Windows 任务，模型 API 可以部署在本机或远程。

从项目根目录，使用已安装 Agent 依赖的 Python 环境启动：

```powershell
python .\osworld_agent_1010\launch.py
```

打开 http://127.0.0.1:8088 。需要其他端口时添加 `--port 8099`。启动器自动识别当前版本目录，无需将 `osworld_agent_1010` 改名。若运行 Agent 的 Python 与 UI 不同，可通过 `OSWORLD_RUNNER_PYTHON` 指定解释器路径。

服务启动和空闲时不进行后台模型保活，即使配置了模型 URL。点击开始任务后，由任务 runner 在规划、决策和必要定位阶段按需调用模型，任务结束后不继续定时请求。

## 配置与运行

1. 填写任务目标，选择“浏览器优先”（默认）或“通用桌面 · 兼容模式”。默认最大执行步数为 20，单步预算 120 秒。
2. 选择或输入决策与定位模型，检查各自的 API 地址。模型预设读取自 `config/model_presets.yaml`；自定义模型可直接输入名称与地址。
3. 在定位模型的“协议与认证”中选择 UI-Venus-2 或 UI-TARS。定位模型保留为视觉兜底；网页节点操作优先走 AX 引用。高级设置提供决策、定位与单步超时，以及 AX 字符预算（默认 5000）和浏览器截图最长边（默认 1280 像素）。字符预算与模型 token 预算独立；截图保持宽高比例，操作定位仍使用原始网页坐标。
4. 任务描述旁提供“开始运行”和“测试模型连接”，无需滚动到运行概览。可先测试决策模型的视觉连接，再开始任务。界面自动打开实时工作区；“停止运行”会终止当前任务并保留已有记录。桌面示例会自动选择通用桌面模式。

任务、模型地址、执行模式和预算会自动保存为浏览器草稿；配置页的 API Key 不进入草稿。系统提示词随模式切换，仅供预览，实际完整输入可在实时调用中查看。

命令行也使用浏览器优先模式：

```powershell
.\.venv\Scripts\python.exe .\osworld_agent_1010\launch.py --runner local --task "搜索 Attention Is All You Need，找到论文并下载 PDF" --execution-mode browser_first --max-steps 20 --ax-max-chars 5000 --screenshot-max-edge 1280 --step-timeout 120
```

`--execution-mode desktop` 使用兼容的通用桌面流程。Chrome 默认路径为 `C:\Program Files\Google\Chrome\Application\chrome.exe`，可用 `--chrome` 指定；浏览器调试端口默认 9222，可用 `--browser-port` 指定。Agent 使用专用无痕浏览器；新启动实例直接复用经身份核验的空白启动页，导航和跨子任务不重复开窗。任务收尾关闭本次拥有的页面，并清理本次新启动的专用实例；复用端点中的其它页面不受影响。

## 实时查看

- 左侧显示模型调用、执行动作、异常与计划更新。可按类型筛选，或搜索输入、输出与动作。
- 点击任意事件、向上滚动轨迹或使用键盘向上翻页会暂停跟随；点击“跟随已暂停”恢复到最新事件。列表按事件 ID 增量更新，不滚动外层工作区；查看早期任务时，调用完成、计划更新与状态轮询不会将滚动条拉回底部。
- 中间显示所选事件的模型输入截图，状态事件使用此前最近一次观察。定位结果按实际截图尺寸标注，点击图片可放大；下方展示最新任务计划。
- “子任务与网页区域”可展开查看当前子任务、ROI 区域、AX 原始与保留节点数、精简后的字符数、截图原始与输入尺寸、选择或恢复原因，以及已记录的阶段耗时。选中早期步骤时显示对应步骤的数据；决策输入与操作后观察分别标注快照，避免将两个阶段的数据混淆。
- AX 状态 `degraded` 表示本次网页结构仍未就绪，决策改用当前浏览器截图并独立核验；同时显示采集次数和有效内容节点数。完成申请被拒后自动重新观察，默认连续 3 次失败停止并记录具体原因，失败结果显示为 `failed`。
- 输入和输出固定同时展示，各自支持滚动和复制。大屏上下排列，小窗口双列排列；完整消息、事件 JSON 与进程日志通过独立浮层查看，不替换输入输出区域，也不改变轨迹和面板的滚动位置。可点击“收起”、再次点击同一个信息按钮或按 Escape 关闭。文本更新保留同一调用的阅读位置。完整消息保留角色与全部文本，图片以引用和元数据表示。模型响应完成后更新原调用，界面不模拟文本流。
- WebSocket 断开后自动重连，并从服务端恢复事件、计划与最近截图。实时截图缓存最多 40 张、32 MiB，启动新任务后清空；过期截图可在已保存的历史轨迹中回看。
- “导出轨迹”导出当前任务的文本事件及图片引用。截图文件继续保存在本机运行目录。

## 历史回看

历史轨迹读取 `local_runs/`，支持按任务描述、运行 ID 与结果筛选。详情保留屏幕、动作、执行结果、备注；记录包含 `policy_input` / `policy_output` 时，可展开完整模型输入与原始输出。“导出完整记录”保存原始 `trajectory.json` 内容，图片资产仍引用运行目录中的文件。

模型对话可单独测试文本与图片请求。回复归属发起请求的会话，切换会话不会改变回复的保存位置。

## 验证范围

从项目根目录运行离线回归：

```powershell
.\.venv\Scripts\python.exe -m pytest .\osworld_agent_1010\tests -q
```

测试自动绑定当前版本目录，无需改名或设置 PYTHONPATH。后端测试覆盖完整输入记录、截图缓存与恢复、停止事件持久化及启动入口。浏览器测试使用模拟 API 与轨迹验证配置、运行控制、事件更新、消息查看、暂停跟随、计划版本、历史读取错误与刷新恢复，并检查桌面和窄窗口布局。验证过程不会操作真实 Windows 桌面或调用远程模型。

`test_browser_first_live.py` 另外启动独立的 headless Chrome 和 localhost 页面，验证完整 AX 缓存、缩略图坐标、精确节点点击、私有会话复用、弹出页面及刷新后拒绝旧引用。测试结束仅清理自己启动的会话和进程。

可重复的 AX 组件对比见 `script/benchmark_browser_context.py` 和 `docs/browser_context_benchmark.json`。该实验使用标注 ROI，比较目标暴露与结构输入量，不代表真实模型端到端成功率。

界面回归另外需要 Node.js 和 Node Playwright；缺少时会明确跳过。可按下面的方式配置：

```powershell
npm install --prefix .ui-test-deps playwright@1.63.0
$env:PLAYWRIGHT_MODULE = (Resolve-Path .ui-test-deps\node_modules\playwright).Path
$env:OSWORLD_UI_TEST_BROWSER = "C:\Program Files\Google\Chrome\Application\chrome.exe"
.\.venv\Scripts\python.exe -m pytest .\osworld_agent_1010\tests\test_workspace_ui.py -q
```
