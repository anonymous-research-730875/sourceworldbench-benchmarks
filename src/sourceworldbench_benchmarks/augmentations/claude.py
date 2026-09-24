import asyncio
import dataclasses
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentation
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID

if TYPE_CHECKING:
    from claude_agent_sdk import HookMatcher, Message, ToolUseBlock

logger = logging.getLogger(__name__)

# Read + edit tools only, no Bash. Every one of these touches the filesystem and
# is gated by `_path_guard`; Bash is excluded so the guard can't be bypassed.
_AGENT_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite"]
_FILE_TOOLS_MATCHER = "Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit"
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_DEFAULT_MODEL = "claude-haiku-4-5"


def _tool_paths(tool_input: dict) -> list[str]:
    """Filesystem paths a tool will touch (empty means it operates on the cwd)."""
    return [tool_input[key] for key in ("file_path", "notebook_path", "path") if tool_input.get(key)]


def _is_within(target: Path, root: Path) -> bool:
    """True if `target` is `root` itself or nested under it."""
    return target == root or root in target.parents


def _path_guard(repo: Path, scope: list[str]) -> "HookMatcher":
    """PreToolUse hook confining every file tool to `repo`, and writes to `scope`.

    Reads/searches must stay inside the repo; writes must additionally stay inside
    `scope` (when given — empty scope means the whole repo is writable). Paths
    (and symlinks) are resolved before checking, so `..` and links can't escape.
    """
    from claude_agent_sdk import HookMatcher

    repo = repo.resolve()
    write_roots = [(repo / entry).resolve() for entry in scope]

    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def guard(input_data: dict, tool_use_id: str | None, context: dict) -> dict:
        tool_name = input_data.get("tool_name", "")
        for raw in _tool_paths(input_data.get("tool_input", {})):
            target = (repo / raw).resolve()
            if not _is_within(target, repo):
                return _deny(f"{raw} is outside the repository ({repo}); all file access must stay within it.")
            if tool_name in _WRITE_TOOLS and write_roots and not any(_is_within(target, r) for r in write_roots):
                return _deny(
                    f"{raw} is outside the allowed write scope ({', '.join(scope)}); only edit files within the scope."
                )
        return {}

    return HookMatcher(matcher=_FILE_TOOLS_MATCHER, hooks=[guard])


def _resolve_api_key(api_key: str | None) -> str:
    """Return the Anthropic API key, requiring it to be present."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AugmentationError("an Anthropic API key is required: pass --api-key or set ANTHROPIC_API_KEY")
    return key


async def _run_agent(
    repo: Path,
    prompt: str,
    scope: list[str],
    model: str,
    max_turns: int | None,
    api_key: str,
    base_url: str | None,
    show_logs: bool,
    transcript: Path | None,
) -> None:
    """Drive the agent over `repo` for a single run, editing files in place.

    Streams progress to stderr when `show_logs`, dumps every message to
    `transcript` (JSONL) when given, and raises AugmentationError if the agent's
    final result is an error.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    except ImportError as exc:
        raise AugmentationError(
            "claude-agent-sdk is not installed. Sync augmentation deps: uv sync --group augmentations"
        ) from exc

    # An empty, throwaway config dir keeps the agent from picking up a
    # subscription login on the machine, so the API key is the only possible credential
    with tempfile.TemporaryDirectory(prefix="sourceworldbench-claude-cfg-") as config_dir:
        env = {"ANTHROPIC_API_KEY": api_key, "CLAUDE_CONFIG_DIR": config_dir}
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        options = ClaudeAgentOptions(
            cwd=str(repo),
            tools=_AGENT_TOOLS,
            permission_mode="bypassPermissions",
            model=model,
            max_turns=max_turns,
            hooks={"PreToolUse": [_path_guard(repo, scope)]},
            env=env,
            stderr=lambda line: logger.debug("claude: %s", line),
        )
        # Record the failure and raise after the generator is exhausted: raising
        # inside the `async for` aclose()s the SDK's still-running generator and
        # crashes with "asynchronous generator is already running". The
        # ResultMessage is the last message, so letting the loop finish is free.
        error: str | None = None
        sink = transcript.open("w", encoding="utf-8") if transcript is not None else None
        try:
            async for message in query(prompt=prompt, options=options):
                if show_logs:
                    _report(message)
                if sink is not None:
                    record = {"_type": type(message).__name__, **dataclasses.asdict(message)}
                    sink.write(json.dumps(record, default=str) + "\n")
                if isinstance(message, ResultMessage) and message.is_error:
                    error = f"claude agent failed ({message.subtype}): {message.result or message.errors}"
        finally:
            if sink is not None:
                sink.close()
                logger.info("wrote transcript to %s", transcript)
    if error is not None:
        raise AugmentationError(error)


def _report(message: "Message") -> None:
    """Print a readable progress line for one agent message to stderr."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                typer.echo(f"claude> {block.text.strip()}", err=True)
            elif isinstance(block, ToolUseBlock):
                typer.echo(f"  • {_format_tool_use(block)}", err=True)
    elif isinstance(message, ResultMessage):
        cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd is not None else "cost n/a"
        typer.echo(f"claude finished: {message.num_turns} turns, {cost}", err=True)


def _format_tool_use(block: "ToolUseBlock") -> str:
    """Compact one-line description of a tool call, e.g. 'Edit src/calc.py'."""
    target = block.input.get("file_path") or block.input.get("notebook_path") or block.input.get("pattern")
    return f"{block.name} {target}" if target else block.name


def _resolve_prompt(prompt: str) -> str:
    """Return the prompt text, reading it from a file when `prompt` is a path to one."""
    path = Path(prompt)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return prompt


def _build_mutate(
    prompt: str,
    scope: list[str],
    model: str,
    max_turns: int | None,
    api_key: str,
    base_url: str | None,
    show_logs: bool,
    transcript: Path | None,
) -> Callable[[Path], None]:
    prompt = _resolve_prompt(prompt)
    transcript = transcript.resolve() if transcript is not None else None

    def mutate(repo: Path) -> None:
        logger.info("running claude agent (cwd=%s, scope=%s)", repo, scope or "<whole repo>")
        asyncio.run(_run_agent(repo, prompt, scope, model, max_turns, api_key, base_url, show_logs, transcript))

    return mutate


def claude_cmd(
    prompt: str = typer.Option(
        ..., "--prompt", help="Instruction for the Claude Code agent, or a path to a file containing it."
    ),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Local or remote Docker image with the repo checked out at /app.",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Path to a clean local git working tree.",
    ),
    base_commit: str | None = typer.Option(
        None,
        "--base-commit",
        help="Git revision to check out before augmenting. Defaults to the current checkout.",
    ),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help="Path(s) the agent is allowed to edit. Repeatable. Empty means the whole repo.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the diff here. Defaults to stdout."),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Anthropic (or gateway) API key. Falls back to ANTHROPIC_API_KEY. Required."
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Anthropic-compatible gateway base URL (e.g., a LiteLLM proxy). "
        "Unset: use ANTHROPIC_BASE_URL from the environment if set, otherwise the Anthropic API directly.",
    ),
    model: str = typer.Option(_DEFAULT_MODEL, "--model", help="Model id to run (any id the endpoint accepts)."),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Cap the agent's turns."),
    show_logs: bool = typer.Option(
        False, "--show-logs/--no-show-logs", help="Stream the agent's narration and tool calls to stderr."
    ),
    transcript: Path | None = typer.Option(
        None, "--transcript", help="Write the full agent conversation (JSONL, incl. tool results) here."
    ),
    restore: bool = typer.Option(
        True,
        "--restore/--no-restore",
        help="Reset the working tree after capturing the diff.",
    ),
) -> None:
    """Run the Claude Code agent over a target repo (`--repo` or `--image`) and emit its diff.

    The agent can only read and edit, it can't run Bash commands.
    All file access is confined to the repo; writes are further limited to `--scope`.
    Auth is by API key only (either pass `--api-key` or use the env variable `ANTHROPIC_API_KEY`);
    point `--base-url` at an Anthropic-compatible gateway (e.g. LiteLLM) to route through it.
    """
    try:
        mutate = _build_mutate(
            prompt, scope, model, max_turns, _resolve_api_key(api_key), base_url, show_logs, transcript
        )
        if repo is not None and image is not None:
            raise AugmentationError("provide only one of --image or --repo")
        if repo is not None:
            run_augmentation(repo=repo, base_commit=base_commit, scope=scope, mutate=mutate, out=out, restore=restore)
        elif image is not None:
            if base_commit is not None:
                raise AugmentationError("--base-commit is not supported with --image")
            with temporary_worktree_from_image(image) as repo_path:
                run_augmentation(repo=repo_path, base_commit=None, scope=scope, mutate=mutate, out=out, restore=False)
        else:
            raise AugmentationError("provide --image or --repo")
    except AugmentationError as exc:
        fail(str(exc))


def claude_hf_cmd(
    base_instance_id: str = typer.Option(
        ..., "--base-instance-id", help="instance_id of the bare base row in the HF dataset."
    ),
    prompt: str = typer.Option(
        ..., "--prompt", help="Instruction for the Claude Code agent, or a path to a file containing it."
    ),
    suffix: str | None = typer.Option(
        None, "--suffix", help="Suffix replacing the base state's `base` segment. Defaults to a UTC timestamp."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing row with the same instance_id."),
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID, "--in", help="Dataset to augment: a local JSONL file or an HF dataset repo id."
    ),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the dataset to the hub after augmenting."),
    out_path: str | None = typer.Option(
        None, "--out", help="Write the resulting dataset to this JSONL path. Skipped when omitted."
    ),
    scope: list[str] = typer.Option(
        [], "--scope", help="Path(s) the agent is allowed to edit. Repeatable. Empty means the whole repo."
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Anthropic (or gateway) API key. Falls back to ANTHROPIC_API_KEY. Required."
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Anthropic-compatible gateway base URL (e.g., a LiteLLM proxy). "
        "Unset: use ANTHROPIC_BASE_URL from the environment if set, otherwise the Anthropic API directly.",
    ),
    model: str = typer.Option(_DEFAULT_MODEL, "--model", help="Model id to run (any id the endpoint accepts)."),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Cap the agent's turns."),
    show_logs: bool = typer.Option(
        False, "--show-logs/--no-show-logs", help="Stream the agent's narration and tool calls to stderr."
    ),
    transcript: Path | None = typer.Option(
        None, "--transcript", help="Write the full agent conversation (JSONL, incl. tool results) here."
    ),
) -> None:
    """Run the agent against an HF row's container and push the diff as a new augmented row.

    Scope, auth, and the other options behave as in `augment-local claude`.
    """
    from sourceworldbench_benchmarks.commands.augment_hf import run_hf_augmentation

    try:
        mutate = _build_mutate(
            prompt, scope, model, max_turns, _resolve_api_key(api_key), base_url, show_logs, transcript
        )
    except AugmentationError as exc:
        fail(str(exc))

    run_hf_augmentation(
        base_instance_id=base_instance_id,
        suffix=suffix,
        overwrite=overwrite,
        scope=scope,
        mutate=mutate,
        in_path=in_path,
        push=push,
        out_path=out_path,
    )
