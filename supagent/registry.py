"""Tools as plain functions: the agent calls them in-process; the MCP server mode
(`superset supagent mcp`) hands the same functions to FastMCP when it is installed."""

from __future__ import annotations

import copy
import inspect
import json
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, create_model


def _inline_refs(schema: dict) -> dict:
    """$ref replaced by the referenced $defs entry (local models read plain schemas better)."""
    defs = schema.get("$defs", {})

    def walk(node: Any, depth: int = 0) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and depth < 8:
                return walk(copy.deepcopy(defs.get(node["$ref"].rsplit("/", 1)[-1], {})), depth + 1)
            return {k: walk(v, depth) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, depth) for v in node]
        return node

    return walk(schema)


class Tool:
    def __init__(self, fn: Callable[..., Any], name: str | None = None) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.description = inspect.cleandoc(fn.__doc__ or "").strip()
        hints = get_type_hints(fn)
        fields: dict[str, Any] = {}
        for pname, p in inspect.signature(fn).parameters.items():
            default = ... if p.default is inspect.Parameter.empty else p.default
            fields[pname] = (hints.get(pname, Any), default)
        self.model = create_model(f"{self.name}_arguments", **fields)  # type: ignore[call-overload]
        schema = _inline_refs(self.model.model_json_schema())
        schema.pop("title", None)
        for prop in (schema.get("properties") or {}).values():
            prop.pop("title", None)
        self.parameters = schema

    def spec(self) -> dict:
        """OpenAI tool definition."""
        return {"type": "function", "function": {"name": self.name, "description": self.description[:1500],
                                                 "parameters": self.parameters}}

    def call(self, arguments: dict[str, Any]) -> Any:
        props = self.parameters.get("properties") or {}
        if list(props) == ["request"] and "request" not in arguments:
            arguments = {"request": arguments}         # models often skip the wrapper object
        values = self.model(**arguments)
        kwargs = {k: getattr(values, k) for k in self.model.model_fields}
        return self.fn(**kwargs)


class Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def tool(self, fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            t = Tool(f, name)
            self.tools[t.name] = t
            return f

        return register(fn) if fn is not None else register

    def call_text(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool and give its result as the text the model reads."""
        result = self.tools[name].call(arguments)
        if isinstance(result, str):
            return result
        if isinstance(result, BaseModel):
            return result.model_dump_json()
        return json.dumps(result, ensure_ascii=False, default=str)
