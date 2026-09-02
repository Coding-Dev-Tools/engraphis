"""First-party Engraphis integration for PrimeIntellect's prime-agent."""
from .config import (
    DEFAULT_AGENT_NAMES,
    EngraphisRuntimeConfig,
    build_runtime_config,
)

__all__ = [
    "EngraphisRuntimeConfig",
    "build_runtime_config",
    "DEFAULT_AGENT_NAMES",
]
__version__ = "0.1.0"

# Defer the heavy imports (mcp_client, tools, agent) so callers that only
# need config or exception types don't have to install the mcp package.
try:  # pragma: no cover - import guard
    from .mcp_client import (
        EngraphisCompatibilityError,
        EngraphisMcpClient,
        EngraphisMcpToolError,
    )
    from .tools import all_tools, apply_scope_defaults, build_tool, TOOL_SPECS
    from .agent import EngraphisPrimeAgent, PrimeAgentFleet

    __all__ += [
        "EngraphisMcpClient",
        "EngraphisMcpToolError",
        "EngraphisCompatibilityError",
        "EngraphisPrimeAgent",
        "PrimeAgentFleet",
        "all_tools",
        "apply_scope_defaults",
        "build_tool",
        "TOOL_SPECS",
    ]
except ImportError:  # mcp (or a transitive dep) is not installed
    pass
