"""The tool contract and the runtime that enforces it.

Every tool declares an argument schema, its approved routes, a timeout, which
statuses may be retried, and a success check. The runtime validates arguments
before the call, bounds the call in time, and classifies the outcome. A tool
never raises into the agent loop: failures come back as a `ToolResult` with a
status the planner can act on.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from ..schemas import ToolCall, ToolResult, ToolStatus


class SimulatedTimeout(Exception):
    """Raised by a mock backend to signal that the route did not answer."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    routes: tuple[str, ...] = ("default",)
    timeout_seconds: float = 5.0
    retryable_statuses: tuple[ToolStatus, ...] = (
        ToolStatus.TIMEOUT,
        ToolStatus.NO_RESULTS,
        ToolStatus.INVALID_ARGS,
    )
    max_retries: int = 2
    idempotent: bool = True
    side_effects: str = "none"
    fallback: str = "return_status_to_planner"

    def json_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "routes": list(self.routes),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "idempotent": self.idempotent,
            "side_effects": self.side_effects,
            "fallback": self.fallback,
            "parameters": self.args_model.model_json_schema(),
        }


class Tool(ABC):
    spec: ToolSpec

    @abstractmethod
    def call(self, args: BaseModel, route: str) -> ToolResult:
        """Execute against the given route. May raise SimulatedTimeout."""

    def success_check(self, result: ToolResult) -> bool:
        """Whether the observation is usable. Overridden where usefulness is
        more than a non-error status."""
        return result.ok


@dataclass
class ToolRuntime:
    """Validates, invokes, and times every tool call."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.spec.name] = tool

    def spec(self, name: str) -> ToolSpec:
        return self.tools[name].spec

    def catalogue(self) -> list[dict]:
        return [tool.spec.json_schema() for tool in self.tools.values()]

    def invoke(self, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.tool)
        if tool is None:
            return ToolResult(
                tool=call.tool,
                route=call.route,
                status=ToolStatus.INVALID_ARGS,
                error=f"unknown tool {call.tool!r}",
            )

        spec = tool.spec
        route = call.route or spec.routes[0]
        if route not in spec.routes:
            return ToolResult(
                tool=spec.name,
                route=route,
                status=ToolStatus.INVALID_ARGS,
                error=f"route {route!r} is not approved for {spec.name}; "
                f"approved routes: {', '.join(spec.routes)}",
            )

        try:
            args = spec.args_model.model_validate(call.args)
        except ValidationError as exc:
            return ToolResult(
                tool=spec.name,
                route=route,
                status=ToolStatus.INVALID_ARGS,
                error="; ".join(
                    f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
                ),
            )

        started = time.perf_counter()
        try:
            result = tool.call(args, route)
        except SimulatedTimeout as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ToolResult(
                tool=spec.name,
                route=route,
                status=ToolStatus.TIMEOUT,
                error=str(exc) or f"route {route!r} did not respond within {spec.timeout_seconds}s",
                latency_ms=elapsed,
            )
        except Exception as exc:  # a misbehaving backend must not end the run
            elapsed = int((time.perf_counter() - started) * 1000)
            return ToolResult(
                tool=spec.name,
                route=route,
                status=ToolStatus.INVALID_ARGS,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=elapsed,
            )

        elapsed_s = time.perf_counter() - started
        result = result.model_copy(update={"latency_ms": int(elapsed_s * 1000), "route": route})

        if elapsed_s > spec.timeout_seconds:
            return result.model_copy(
                update={
                    "status": ToolStatus.TIMEOUT,
                    "error": f"exceeded {spec.timeout_seconds}s budget",
                    "evidence": [],
                }
            )

        if result.ok and not tool.success_check(result):
            return result.model_copy(
                update={
                    "status": ToolStatus.NO_RESULTS,
                    "error": result.error or "result did not pass the tool's success check",
                }
            )
        return result
