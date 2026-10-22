"""对比固定 Git 基线与工作区实现；仅使用合成消息和确定性 Host 替身。

从 LocalAgent 目录执行：
    uv run --no-sync python benchmarks/context_token_counts.py

耗时包含消息复制、序列化与模拟 RPC 等待，不代表真实模型或线上吞吐。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import types
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from langbot_plugin.api.entities.builtin.provider.message import ContentElement, Message
from pydantic import BaseModel

DEFAULT_BASELINE = "7b532cfd6222aa79f05f113b4db6c876d4782434"
SOURCE_PATH = "Runner/LocalAgent/pkg/context_pipeline.py"
AGENT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Scenario:
    name: str
    budget: dict[str, int]
    inputs: dict[str, list[Message]]
    expect_compaction: bool = False
    raw_attachment_bytes: int = 0
    base64_attachment_bytes: int = 0


@dataclass(frozen=True)
class Observation:
    elapsed_ms: float
    host_calls: int
    result: Any


class CountingHost:
    """按当前任务统计调用；空消息也收取 7 个模拟 token 的请求开销。"""

    def __init__(self, delay_ms: float):
        self.delay_seconds = delay_ms / 1000
        self.calls: dict[asyncio.Task, int] = {}

    async def count_tokens(self, *, messages: list[Message], **kwargs: Any) -> int:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("The counting stub must run inside an asyncio task")
        self.calls[task] = self.calls.get(task, 0) + 1
        # 0 ms 仍交还事件循环，让并发组装交错执行。
        await asyncio.sleep(self.delay_seconds)
        return 7 + sum(len(message.model_dump_json(exclude_none=True)) for message in messages)


def git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(AGENT_ROOT), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def load_implementations(baseline_ref: str) -> tuple[types.ModuleType, types.ModuleType, dict[str, Any]]:
    baseline_commit = git("rev-parse", "--verify", f"{baseline_ref}^{{commit}}").strip()
    baseline_source = git("show", f"{baseline_commit}:{SOURCE_PATH}")
    sys.path.insert(0, str(AGENT_ROOT))
    current = importlib.import_module("pkg.context_pipeline")

    # 独立模块名保留 dataclass / typing 的正常解析；不复制或改写历史实现。
    name = "pkg._context_pipeline_benchmark_baseline"
    baseline = types.ModuleType(name)
    baseline.__file__ = f"<git:{baseline_commit}:{SOURCE_PATH}>"
    sys.modules[name] = baseline
    exec(compile(baseline_source, baseline.__file__, "exec"), baseline.__dict__)

    current_source = (AGENT_ROOT / "pkg" / "context_pipeline.py").read_bytes()
    metadata = {
        "baseline": {
            "requested_ref": baseline_ref,
            "commit": baseline_commit,
            "source_sha256": hashlib.sha256(baseline_source.encode("utf-8")).hexdigest(),
        },
        "current": {
            "head_commit": git("rev-parse", "HEAD").strip(),
            "working_tree_source_modified": bool(git("status", "--porcelain", "--", "pkg/context_pipeline.py")),
            "source_sha256": hashlib.sha256(current_source).hexdigest(),
        },
        "source_path": SOURCE_PATH,
    }
    return baseline, current, metadata


def scenarios() -> list[Scenario]:
    prompt = [Message(role="system", content="Answer accurately and preserve the current request.")]
    current = [Message(role="user", content="Summarize the available context and suggest a next step.")]
    attachment = base64.b64encode(bytes(256 * 1024)).decode("ascii")
    return [
        Scenario("plain_text", {}, {"prompt_messages": prompt, "history_messages": [], "current_messages": current}),
        Scenario(
            "history_and_rag",
            {},
            {
                "prompt_messages": prompt,
                "history_messages": [
                    Message(role="user" if index % 2 == 0 else "assistant", content=f"Turn {index}: " + "context " * 30)
                    for index in range(12)
                ],
                "rag_messages": [
                    Message(role="user", content=f"Reference {index}: " + "evidence " * 30) for index in range(3)
                ],
                "current_messages": current,
            },
        ),
        Scenario(
            "compaction",
            {"window_tokens": 1400, "reserve_tokens": 100, "keep_recent_tokens": 200, "summary_tokens": 250},
            {
                "prompt_messages": prompt,
                "history_messages": [
                    Message(
                        role="user" if index % 2 == 0 else "assistant", content=f"Turn {index}: " + "background " * 50
                    )
                    for index in range(12)
                ],
                "rag_messages": [Message(role="user", content="Retrieved evidence " * 40)],
                "current_messages": current,
            },
            expect_compaction=True,
        ),
        Scenario(
            "multimodal_256_kib",
            {"window_tokens": 1_000_000, "reserve_tokens": 4096},
            {
                "prompt_messages": prompt,
                "history_messages": [],
                "current_messages": [
                    Message(
                        role="user",
                        content=[
                            ContentElement.from_text("Inspect this synthetic attachment."),
                            ContentElement.from_image_base64(attachment),
                        ],
                    )
                ],
            },
            raw_attachment_bytes=256 * 1024,
            base64_attachment_bytes=len(attachment),
        ),
    ]


def snapshot(value: Any) -> Any:
    """按值比较跨独立模块的完整结果，避免 dataclass 类身份影响比较。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: snapshot(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(item) for item in value]
    return value


def distribution(values: list[float | int]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "sample_count": len(values),
        "minimum": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "maximum": ordered[-1],
    }


async def measure_batch(compactor: Any, host: CountingHost, scenario: Scenario, concurrency: int) -> list[Observation]:
    async def assemble() -> Observation:
        task = asyncio.current_task()
        started = time.perf_counter()
        result = await compactor.compact_async(**scenario.inputs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        calls = host.calls.pop(task, 0)
        return Observation(elapsed_ms, calls, result)

    return await asyncio.gather(*(assemble() for _ in range(concurrency)))


async def benchmark_case(
    baseline: types.ModuleType,
    current: types.ModuleType,
    scenario: Scenario,
    *,
    delay_ms: float,
    concurrency: int,
    samples: int,
    warmup: int,
) -> dict[str, Any]:
    measurements: dict[str, list[Observation]] = {"baseline": [], "current": []}
    implementations = {"baseline": baseline, "current": current}
    hosts = {name: CountingHost(delay_ms) for name in implementations}
    compactors = {
        name: module.ContextCompactor(
            module.ContextBudget(**scenario.budget),
            token_counter=module.HostContextTokenCounter(hosts[name], "synthetic-benchmark-model"),
        )
        for name, module in implementations.items()
    }
    expected_result = None
    for batch in range(warmup + samples):
        # 交替执行顺序，减少固定先后次序对耗时的影响。
        names = ["baseline", "current"] if batch % 2 == 0 else ["current", "baseline"]
        for name in names:
            observations = await measure_batch(compactors[name], hosts[name], scenario, concurrency)
            for observation in observations:
                result = snapshot(observation.result)
                if expected_result is None:
                    expected_result = result
                if result != expected_result:
                    raise RuntimeError(f"Semantic mismatch in {scenario.name} ({name}, batch {batch})")
                if result["compacted"] != scenario.expect_compaction:
                    raise RuntimeError(f"Unexpected compaction path in {scenario.name}")
            if batch >= warmup:
                measurements[name].extend(observations)

    results = {
        name: {
            "assembly_elapsed_ms": distribution([observation.elapsed_ms for observation in observations]),
            "host_calls_per_assembly": distribution([observation.host_calls for observation in observations]),
            "host_calls_total": sum(observation.host_calls for observation in observations),
        }
        for name, observations in measurements.items()
    }
    old_calls = results["baseline"]["host_calls_total"]
    new_calls = results["current"]["host_calls_total"]
    old_median = results["baseline"]["assembly_elapsed_ms"]["median"]
    new_median = results["current"]["assembly_elapsed_ms"]["median"]
    return {
        "scenario": scenario.name,
        "simulated_rpc_delay_ms": delay_ms,
        "concurrency": concurrency,
        "measured_batches": samples,
        "warmup_batches_excluded": warmup,
        "semantic_equivalence": True,
        "compacted": scenario.expect_compaction,
        "raw_attachment_bytes": scenario.raw_attachment_bytes,
        "base64_attachment_bytes": scenario.base64_attachment_bytes,
        **results,
        "host_call_reduction_percent": 100 * (old_calls - new_calls) / old_calls,
        "median_elapsed_reduction_percent": 100 * (old_median - new_median) / old_median,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE)
    parser.add_argument("--samples", type=int, default=5, help="每种配置的计时批次数；每批执行 concurrency 次组装")
    parser.add_argument("--warmup", type=int, default=1, help="每种配置预热批次数，不计入耗时统计")
    parser.add_argument(
        "--delays-ms", nargs="+", type=float, default=[0, 2, 10], help="每次 Host 计数调用的模拟等待毫秒数"
    )
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 8], help="同一 compactor 上并发执行的组装数")
    args = parser.parse_args()
    if args.samples < 1 or args.warmup < 0:
        parser.error("--samples must be positive and --warmup must be nonnegative")
    if any(not math.isfinite(value) or value < 0 for value in args.delays_ms):
        parser.error("--delays-ms values must be finite and nonnegative")
    if any(value < 1 for value in args.concurrency):
        parser.error("--concurrency values must be positive")
    return args


async def main(args: argparse.Namespace) -> dict[str, Any]:
    baseline, current, implementation_metadata = load_implementations(args.baseline_ref)
    results = []
    for scenario in scenarios():
        for delay_ms in args.delays_ms:
            for concurrency in args.concurrency:
                results.append(
                    await benchmark_case(
                        baseline,
                        current,
                        scenario,
                        delay_ms=delay_ms,
                        concurrency=concurrency,
                        samples=args.samples,
                        warmup=args.warmup,
                    )
                )
    return {
        "schema_version": 1,
        "benchmark": "assembly_scoped_token_count_deduplication",
        "measurement_scope": "synthetic_host_calls_and_local_assembly_latency_not_production_throughput",
        "counting_stub": "7 + total Message.model_dump_json(exclude_none=True) character length; asyncio.sleep RPC delay",
        "p95_method": "nearest_rank",
        "semantic_equivalence": True,
        "implementations": implementation_metadata,
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "langbot_plugin_sdk": importlib.metadata.version("langbot-plugin"),
            "pydantic": importlib.metadata.version("pydantic"),
        },
        "results": results,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(parse_arguments())), ensure_ascii=False, indent=2))
