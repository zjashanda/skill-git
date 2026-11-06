import sys
from pathlib import Path

scripts = Path(__file__).resolve().parents[1] / "scripts"
if str(scripts) not in sys.path:
    sys.path.insert(0, str(scripts))
