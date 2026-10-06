"""Which approval policy applies to a tool."""

from agentkit.config.schema import ApprovalPolicy, SideEffects, ToolApproval


def resolve_policy(
    tool_name: str, side_effects: SideEffects, approval: ToolApproval
) -> ApprovalPolicy:
    """Most specific first: per-tool override, the side-effect class, the global default.

    A destructive tool is only ever allowed by its own per-tool override.
    """
    if tool_name in approval.overrides:
        return approval.overrides[tool_name]
    policy = approval.by_side_effects.get(side_effects, approval.default)
    if side_effects == "destructive" and policy == "allow":
        return "require"
    return policy
