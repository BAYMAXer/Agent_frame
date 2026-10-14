# Agent_frame

Windows 本机浏览器与桌面 Agent 框架。默认采用浏览器优先流程，保留通用 Windows 桌面兼容模式。

当前版本：**0.3.4**（[v0.3.4 Release](https://github.com/BAYMAXer/Agent_frame/releases/tag/v0.3.4)）。

源码直接位于仓库根目录，不同版本通过 Git 标签保存。本版本的发布源码不包含测试文件。

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
| [v0.3.4](https://github.com/BAYMAXer/Agent_frame/tree/v0.3.4) | osworld_agent_1010 当前版本，不含测试文件 |
