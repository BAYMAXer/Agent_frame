"""Launch this version's local UI and runners without renaming its package folder.

Run ``python osworld_agent_1010/launch.py`` from the workspace, or
``python launch.py`` from this directory. Starting the UI makes no model calls;
desktop actions begin only after the user starts a task from the interface.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import runpy
import sys


def bootstrap_package():
    """Resolve the canonical imports to this launcher's version directory."""
    package_root = Path(__file__).resolve().parent
    existing = sys.modules.get("osworld_agent")
    if existing is not None:
        if Path(existing.__file__).resolve().parent != package_root:
            raise RuntimeError("A different osworld_agent version is already imported")
        return existing
    spec = importlib.util.spec_from_file_location(
        "osworld_agent", package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["osworld_agent"] = package
    spec.loader.exec_module(package)
    return package


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    bootstrap_package()
    if arguments[:2] == ["--runner", "local"]:
        # Run the unchanged entry point with its existing argument parser.
        sys.argv = [str(Path(__file__).parent / "run_windows_local.py"), *arguments[2:]]
        runpy.run_module("osworld_agent.run_windows_local", run_name="__main__")
        return

    parser = argparse.ArgumentParser(description="Start the local Windows Agent visualization UI")
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENTS_VIZ_PORT", "8088")))
    args = parser.parse_args(arguments)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    os.environ["AGENTS_VIZ_PORT"] = str(args.port)
    os.environ.setdefault("OSWORLD_RUNNER_PYTHON", sys.executable)
    from osworld_agent.viz.server import app
    import uvicorn
    print(f"Agent Studio: http://127.0.0.1:{args.port}", flush=True)
    # Importing the app bypasses server.__main__ and its remote model warm-up.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
