"""本地 Windows 命令行入口，与 run_windows_local 共用参数和执行流程。

通过 ``python launch.py --runner local --task ...`` 启动。
"""
from .run_windows_local import main


if __name__ == "__main__":
    raise SystemExit(main())
