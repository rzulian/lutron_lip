"""Test configuration."""

import sys
from pathlib import Path

# aiolip is self-contained, so import it directly rather than through the
# integration package, which would pull in Home Assistant.
sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "lutron_lip"))
