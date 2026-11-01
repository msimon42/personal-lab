"""Compatibility import for extracted generic infrastructure."""
import sys
from toolkit_core import storage as _implementation
sys.modules[__name__] = _implementation
