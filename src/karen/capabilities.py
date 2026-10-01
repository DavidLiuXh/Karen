"""Authorize the capabilities registered with the task engine."""

from dynamic_graph import DynamicGraphEngine, ExecutionPolicy


def all_capabilities_policy(engine: DynamicGraphEngine) -> ExecutionPolicy:
    capabilities = engine.list_capabilities()
    return ExecutionPolicy(
        allowed_tools=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "tool"],
        allowed_side_effect_tools=[
            f"{c.name}@{c.version}"
            for c in capabilities
            if c.kind == "tool" and c.to_dict()["read_only"] is False
        ],
        allowed_evaluators=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "check"],
        allowed_reducers=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "reducer"],
    )
