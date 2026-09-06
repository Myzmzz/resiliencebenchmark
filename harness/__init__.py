"""Harness package marker.

Imports stay side-effect free so the minimal agent-runtime image can import
``harness.agent_exec`` without loading Controller-side streaming dependencies.
Consumers import the required submodule directly.
"""
