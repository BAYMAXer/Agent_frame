# Agent_frame

Windows 本机浏览器与桌面 Agent 框架。默认采用浏览器优先流程，保留通用 Windows 桌面兼容模式。

当前版本：**0.4.0**（[v0.4.0 Release](https://github.com/BAYMAXer/Agent_frame/releases/tag/v0.4.0)）。

源码直接位于仓库根目录，不同版本通过 Git 标签保存。本版本的发布源码不包含测试文件。

## v0.4.0 本次提交解决的问题

相对于 [v0.3.5](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.5)，本次提交修正以下问题：

| 问题 | 本次处理 |
| --- | --- |
| 规划在少量纠错后就结束，尝试次数无法按模型情况调整。 | 新增规划尝试预算，默认 5 次，可设置为 1–10 次；反馈包含具体校验错误和修正要求，临时请求失败可重试，所有尝试共享规划总超时。 |
| “不需要打开 PDF 查看页”等否定说明、跨句描述被误判为预览或文件下载要求。 | 按句子和否定范围识别完成判据，规范明确的 PDF 链接确认步骤；复合保存目标仍要求程序验证文件落盘，保留论文身份与来源条件。 |
| 模型服务明确不支持 JSON 模式或扩展参数时请求失败；认证、地址或参数错误也被当作可重试故障。 | 在原请求预算内进行一次明确的兼容降级，并记录不支持的参数；永久请求错误及时结束规划，保留错误及每次尝试记录。 |
| 浏览器只能采用固定的无痕流程，所选模式与主任务、子任务的会话使用不能统一。 | 工作台、配置和命令行支持普通／无痕模式，默认无痕；绑定、复用与验证所选模式的任务页面，主任务与子任务共用该会话。 |
| 原生浏览器上下文与下载管理接口不匹配；无痕下载正文停滞时，无法可靠按单步预算取消。 | 适配普通上下文的下载接口；原生无痕模式通过可取消的 CDP 响应流下载，继续沿用 Chrome 网络会话，核验文件大小与 PDF 文件头并清理未完成文件。 |
| 恢复动作后仍累计旧停滞次数，或打开已确认的新任务页后立刻被重复动作检测停止。 | 停滞阈值只统计连续停滞；确认新页的归属并绑定后清理旧重复状态，先观察新页再继续判断。 |

可在工作台选择“浏览器模式”，在高级设置调整“规划尝试次数”；命令行分别使用 `--browser-mode normal` 或 `--browser-mode incognito`，规划次数可使用 `--planner-attempts 5`。

发布前以固定依赖版本完成 **1459 项本地回归**，覆盖普通／无痕 Chrome 下载、下载超时取消、模型错误模拟、规划纠错、停滞恢复、历史 AX 回放及工作台浏览器检查。测试代码与诊断样本继续排除在发布源码之外。

[查看完整提交差异](https://github.com/BAYMAXer/Agent_frame/compare/v0.3.5...v0.4.0)

## 安装与启动

在 Windows 上使用 Python 3.12 和已安装的 Google Chrome，在仓库根目录执行：

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch.py
~~~

打开 http://127.0.0.1:8088 。启动器会自动识别当前目录，无需更改仓库目录名称。模型 API 可部署在本机或远程。

- [Windows 可视化工作台说明](docs/Windows可视化工作台.md)
- [本地环境搭建指南](docs/本地环境搭建指南.md)
- [浏览器上下文对比结果](docs/browser_context_benchmark.json)

## 版本

| 版本 | 对应源码 |
| --- | --- |
| [v0.1.0](https://github.com/BAYMAXer/Agent_frame/tree/v0.1.0) | 原 osworld_agent_1009 代码 |
| [v0.2.0](https://github.com/BAYMAXer/Agent_frame/tree/v0.2.0) | osworld_agent_1010 初始版本 |
| [v0.2.1](https://github.com/BAYMAXer/Agent_frame/tree/v0.2.1) | osworld_agent_1010 的 0.2.1 版本 |
| [v0.2.2](https://github.com/BAYMAXer/Agent_frame/tree/v0.2.2) | osworld_agent_1010 的 0.2.2 版本，不含测试文件 |
| [v0.2.3](https://github.com/BAYMAXer/Agent_frame/tree/v0.2.3) | osworld_agent_1010 的 0.2.3 版本，不含测试文件 |
| [v0.3.0](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.0) | osworld_agent_1010 的 0.3.0 版本，不含测试文件 |
| [v0.3.1](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.1) | osworld_agent_1010 的 0.3.1 版本，不含测试文件 |
| [v0.3.2](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.2) | osworld_agent_1010 的 0.3.2 版本，不含测试文件 |
| [v0.3.3](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.3) | osworld_agent_1010 的 0.3.3 版本，不含测试文件 |
| [v0.3.4](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.4) | osworld_agent_1010 的 0.3.4 版本，不含测试文件 |
| [v0.3.5](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.5) | osworld_agent_1010 的 0.3.5 版本，不含测试文件 |
| [v0.4.0](https://github.com/BAYMAXer/Agent_frame/tree/v0.4.0) | osworld_agent_1010 当前版本，不含测试文件 |
