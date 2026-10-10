import sys
from pathlib import Path

# 让 `import osworld_agent` 可用（包在仓库根 D:\400-project\osworld_agent）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
