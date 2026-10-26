"""Authorize the capabilities registered with the task engine."""

from dynamic_graph import DynamicGraphEngine, ExecutionPolicy


def all_capabilities_policy(engine: DynamicGraphEngine) -> ExecutionPolicy:
    capabilities = engine.list_capabilities()
    return ExecutionPolicy(
        # A content-generation node may use up to three minutes, but all planning,
        # tools, retries and workers still share the ten-minute execution deadline.
        node_timeout_seconds=180,
        run_timeout_seconds=600,
        allowed_tools=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "tool"],
        allowed_side_effect_tools=[
            f"{c.name}@{c.version}"
            for c in capabilities
            if c.kind == "tool" and c.to_dict()["read_only"] is False
        ],
        allowed_evaluators=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "check"],
        allowed_reducers=[f"{c.name}@{c.version}" for c in capabilities if c.kind == "reducer"],
    )
