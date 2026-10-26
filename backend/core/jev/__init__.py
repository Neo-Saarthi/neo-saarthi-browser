import sys
from pathlib import Path

_JEV_DIR: str = str(Path(__file__).resolve().parent)
if _JEV_DIR not in sys.path:
    sys.path.insert(0, _JEV_DIR)
