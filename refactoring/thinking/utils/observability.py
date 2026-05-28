from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("thinking.metrics")


@dataclass
class NodeMetrics:
    node_name: str
    duration_ms: float = 0.0
    token_estimate: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)


class WorkflowMetricsCollector:
    def __init__(self):
        self._node_calls: list[NodeMetrics] = []

    @contextmanager
    def track_node(self, node_name: str):
        start = time.perf_counter()
        metrics = NodeMetrics(node_name=node_name)
        try:
            yield metrics
        except Exception as e:
            metrics.error = str(e)
            raise
        finally:
            metrics.duration_ms = (time.perf_counter() - start) * 1000
            self._node_calls.append(metrics)
            level = logging.WARNING if metrics.error else logging.INFO
            logger.log(
                level,
                "node=%s duration=%.1fms tokens≈%d%s",
                metrics.node_name,
                metrics.duration_ms,
                metrics.token_estimate,
                f" error={metrics.error}" if metrics.error else "",
            )

    def get_summary(self) -> dict:
        if not self._node_calls:
            return {}
        total_ms = sum(m.duration_ms for m in self._node_calls)
        total_tokens = sum(m.token_estimate for m in self._node_calls)
        errors = [m for m in self._node_calls if m.error]
        return {
            "total_nodes": len(self._node_calls),
            "total_duration_ms": round(total_ms, 1),
            "total_tokens_estimate": total_tokens,
            "error_count": len(errors),
            "nodes": [
                {
                    "name": m.node_name,
                    "duration_ms": round(m.duration_ms, 1),
                    "tokens": m.token_estimate,
                    "error": m.error,
                }
                for m in self._node_calls
            ],
        }

    def reset(self):
        self._node_calls.clear()


_current_collector = WorkflowMetricsCollector()


def get_metrics_collector() -> WorkflowMetricsCollector:
    return _current_collector
