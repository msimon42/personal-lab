"""Compatibility import for extracted generic infrastructure."""
import sys
from toolkit_core import backups as _implementation
sys.modules[__name__] = _implementation
