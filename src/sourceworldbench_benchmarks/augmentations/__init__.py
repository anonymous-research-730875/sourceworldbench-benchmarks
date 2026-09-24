from collections.abc import Callable

from sourceworldbench_benchmarks.augmentations.black import black_cmd, black_hf_cmd
from sourceworldbench_benchmarks.augmentations.claude import claude_cmd, claude_hf_cmd
from sourceworldbench_benchmarks.augmentations.cosmic_ray import cosmic_ray_cmd, cosmic_ray_hf_cmd
from sourceworldbench_benchmarks.augmentations.git_history_main import git_history_main_cmd, git_history_main_hf_cmd
from sourceworldbench_benchmarks.augmentations.mutpy import mutpy_cmd, mutpy_hf_cmd
from sourceworldbench_benchmarks.augmentations.ruff import ruff_cmd, ruff_hf_cmd

COMMANDS: dict[str, Callable[..., None]] = {
    "black": black_cmd,
    "claude": claude_cmd,
    "cosmic-ray": cosmic_ray_cmd,
    "git-history-main": git_history_main_cmd,
    "mutpy": mutpy_cmd,
    "ruff": ruff_cmd,
}

HF_COMMANDS: dict[str, Callable[..., None]] = {
    "black": black_hf_cmd,
    "claude": claude_hf_cmd,
    "cosmic-ray": cosmic_ray_hf_cmd,
    "git-history-main": git_history_main_hf_cmd,
    "mutpy": mutpy_hf_cmd,
    "ruff": ruff_hf_cmd,
}
