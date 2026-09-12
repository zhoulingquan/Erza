"""Tool registry for dynamic tool management."""

from typing import Any, Callable

from erza.tools.base import Tool

#: Retry guidance appended to a failed tool result for the model.
RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"

_ERROR_WORD = "Error"


def is_tool_error_payload(value: Any) -> bool:
    """Return True when a tool result is its own error message.

    Tool implementations report failure as a string beginning with ``Error``
    (``"Error: …"``, ``"Error reading file: …"``).  A bare ``startswith`` also
    flags *legitimate* content that merely begins with the same letters —
    ``read_file`` on a file starting with "Errors in the log" is a real case —
    so the word must be followed by a delimiter (end of string, ``:``, or
    whitespace).  ``"Errors…"``/``"Error-prone…"`` are therefore treated as
    content, while every established error form still matches.
    """
    if not isinstance(value, str) or not value.startswith(_ERROR_WORD):
        return False
    remainder = value[len(_ERROR_WORD) :]
    return not remainder or remainder[0] in ": \t\n\r"


def with_retry_hint(text: str) -> str:
    """Append :data:`RETRY_HINT` unless it is already present.

    The registry and the agent's tool-execution layer both decorate failures;
    appending blindly would emit the guidance twice for the same result.
    """
    return text if RETRY_HINT in text else text + RETRY_HINT


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> None:
        """Register a tool and its aliases."""
        self._tools[tool.name] = tool
        for alias in tool.aliases:
            self._aliases[alias] = tool.name
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name (also removes its aliases)."""
        tool = self._tools.pop(name, None)
        if tool:
            for alias in tool.aliases:
                self._aliases.pop(alias, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name or alias."""
        if name in self._tools:
            return self._tools[name]
        primary = self._aliases.get(name)
        if primary:
            return self._tools.get(primary)
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: dict[str, Any],
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        """Resolve, cast, and validate one tool call."""
        # Guard against invalid parameter types (e.g., list instead of dict)
        if not isinstance(params, dict) and name in ("write_file", "read_file"):
            return (
                None,
                params,
                (
                    f"Error: Tool '{name}' parameters must be a JSON object, got {type(params).__name__}. "
                    'Use named parameters: tool_name(param1="value1", param2="value2")'
                ),
            )

        tool = self.get(name)
        if not tool:
            return (
                None,
                params,
                (f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"),
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return (
                tool,
                cast_params,
                (f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)),
            )
        return tool, cast_params, None

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        tool, params, error = self.prepare_call(name, params)
        if error:
            return with_retry_hint(error)

        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            if is_tool_error_payload(result):
                return with_retry_hint(result)
            return result
        except Exception as e:
            return with_retry_hint(f"Error executing {name}: {str(e)}")

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


class LazyToolRegistry(ToolRegistry):
    """A :class:`ToolRegistry` that runs a load hook on first read.

    ``register`` / ``unregister`` never trigger loading, so MCP tools can be
    connected before built-ins are materialized; the first read performs the
    load at most once. ``_loaded`` is set before the hook runs so a hook that
    itself calls ``register`` / ``has`` does not recurse into loading.
    """

    def __init__(self, load_hook: Callable[[], None]) -> None:
        super().__init__()
        self._load_hook = load_hook
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._load_hook()

    def get(self, name: str) -> Tool | None:
        self._ensure_loaded()
        return super().get(name)

    def has(self, name: str) -> bool:
        self._ensure_loaded()
        return super().has(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return super().get_definitions()

    def prepare_call(
        self, name: str, params: dict[str, Any]
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        self._ensure_loaded()
        return super().prepare_call(name, params)

    @property
    def tool_names(self) -> list[str]:
        # ``_set_tool_context`` iterates the full tool set to propagate per-turn
        # routing context and must observe loaded tools. Lazy loading is
        # best-effort, but correctness of tool-context propagation is not.
        self._ensure_loaded()
        return list(self._tools.keys())
