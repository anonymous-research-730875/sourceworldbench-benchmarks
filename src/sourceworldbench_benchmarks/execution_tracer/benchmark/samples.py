"""BenchmarkSample dataclass and JSON (de)serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TASKS = [
    "combined",
    "outcome",
    "peak_rss",
    "wall_time",
    "hot_methods_time",
    "hot_lines_time",
    "hot_methods_alloc",
    "hot_lines_alloc",
]


@dataclass
class SampleContext:
    test_file_path: str
    test_file_content: str
    source_files: dict          # rel_path -> content
    context_strategy: str = "oracle"


@dataclass
class BenchmarkSample:
    sample_id: str
    task: str
    instance_id: str
    test_nodeid: str
    repo: str
    base_commit: str
    context: SampleContext
    system_prompt: str
    user_prompt: str
    ground_truth: Any
    metric: str
    metric_args: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BenchmarkSample":
        ctx = d.pop("context")
        d["context"] = SampleContext(**ctx)
        return cls(**d)


def make_sample_id(instance_id: str, test_nodeid: str, task: str,
                   side: str = "post") -> str:
    h = hashlib.sha1(test_nodeid.encode("utf-8")).hexdigest()[:10]
    return f"{instance_id}::{h}::{task}::{side}"


def write_sample(sample: BenchmarkSample, base_dir: Path) -> Path:
    out_dir = base_dir / sample.task
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sample.sample_id}.json"
    out.write_text(json.dumps(sample.to_dict(), indent=2))
    return out


def read_sample(path: Path) -> BenchmarkSample:
    return BenchmarkSample.from_dict(json.loads(path.read_text()))
