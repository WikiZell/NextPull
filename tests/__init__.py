"""Makes ``py -3 -m unittest tests.test_x`` work as well as ``discover -s tests``: puts the project root and this folder on the path."""
import sys
from pathlib import Path

for _entry in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))
