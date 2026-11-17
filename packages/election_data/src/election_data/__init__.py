"""Auditable observed US election results: archive, normalize, validate, release.

The package depends only on Pydantic and the standard library. It never imports
MCP or host adapters; network access happens only in explicit acquisition calls.
"""
__version__ = "0.1.0"
SCHEMA_VERSION = 1
