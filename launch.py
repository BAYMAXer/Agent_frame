"""Run this source snapshot without requiring its folder to be named osworld_agent."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import runpy
import sys


def bootstrap() -> None:
    root = Path(__file__).resolve().parent
    existing = sys.modules.get("osworld_agent")
    if existing is not None:
        if Path(existing.__file__).resolve() != root / "__init__.py":
            raise RuntimeError("A different osworld_agent package is already loaded")
        return
    spec = importlib.util.spec_from_file_location(
        "osworld_agent", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the osworld_agent package from this directory")
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].startswith("osworld_agent."):
        raise SystemExit("Usage: python launch.py osworld_agent.<module> [arguments]")
    module = sys.argv.pop(1)
    bootstrap()
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
