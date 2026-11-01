"""Compatibility import for extracted generic infrastructure."""
import sys
from toolkit_core import safety as _implementation
sys.modules[__name__] = _implementation
