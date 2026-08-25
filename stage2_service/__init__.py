"""Single-service runtime for fixed-Episode resilience-agent campaigns.

Import concrete modules directly. Keeping package import side-effect free is
required by the isolated BladeAI worker, whose venv intentionally contains
BladeAI dependencies rather than the Controller's full dependency graph.
"""
