"""Identify the *guaranteed* golden commit of a SWE-rebench datapoint.

A SWE-rebench row stores ``patch`` (the fix) and ``test_patch`` (the tests), but
nothing in the row proves which upstream commit these came from.  The
``instance_id`` suffix (``owner__repo-<N>``) is only a naming *convention* — it is
not verified against GitHub.

:func:`identify_golden_commit` establishes ground truth instead of trusting the
convention:

1. The ``<N>`` from the ``instance_id`` is treated as a *candidate* PR number.
2. That PR is fetched from GitHub and must be **merged** and branch from exactly
   the row's ``base_commit`` (``pr.base.sha == base_commit``).  A 40-char SHA is a
   near-unique anchor, so this confirms the suffix really points at this task's PR.
3. The row's fix + test patches, combined, must **exactly** equal the golden
   change, compared by ``git patch-id`` (insensitive to line offsets and file/hunk
   ordering, so equality means byte-identical content changes).  A match against
   *either* of two references counts: the net change landed on ``base_commit``
   (``compare(base_commit...merge_commit_sha)``) or the PR's own diff
   (``pulls/{n}.diff``).  SWE-rebench's stored diff equals one or the other
   depending on the PR's commit/merge structure — GitHub computes ``pulls/{n}.diff``
   as a *merge-base→head* diff, which diverges from the landed change for
   multi-commit PRs — so both are accepted.

Only when all three hold is the PR's ``merge_commit_sha`` returned (a plain
``str``).  Every other outcome returns a :class:`GoldenCommitError` naming the
exact step that failed, so callers can tell (e.g.) "PR not merged" from "diff
mismatch" — use ``isinstance(result, GoldenCommitError)`` to branch.  False
negatives are acceptable by design — the function never returns an unverified
commit.

Each identification makes two REST API round-trips (PR metadata + the landed
diff), and a third only when the landed diff does not match and the PR diff is
tried as a fallback.  All hit ``api.github.com`` and thus share the authenticated
5,000/hr quota — sustaining ~2,000–2,500 items/hr.  Two things keep bulk runs fast
within that budget:

* a per-thread keep-alive :class:`requests.Session` (with retry/backoff for
  transient 5xx/network errors) reuses TCP+TLS connections instead of
  re-handshaking every request;
* :func:`identify_golden_commits` fans datapoints out across a thread pool, which
  is the right model for I/O-bound work (the GIL is released during socket waits).

Rate limiting is treated as an environmental condition, not a failure: when the
quota is exhausted the request **waits for the reset** (``X-RateLimit-Reset`` for
the primary quota, ``Retry-After`` for secondary limits) and retries until it
clears, so a batch pauses rather than burning through every remaining item.
"""

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from rich.progress import track
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Load ``GITHUB_TOKEN`` / ``GH_TOKEN`` (and anything else) from a local ``.env`` on
# import, so the token below is picked up no matter how identification is invoked
# (CLI, ``to_datapoint``, ``rebench_processing``, notebooks). ``load_dotenv`` never
# overrides a variable already set in the real environment. See ``.env.template``.
load_dotenv()

GITHUB_API = "https://api.github.com"

# Precomputed golden commits keyed by ``instance_id``. Identification hits GitHub's
# rate-limited API, so a hit here skips the network round-trips entirely. The file
# holds one ``{"instance_id": ..., "golden_commit": <sha>}`` object per line and is
# authoritative: an entry means the commit was verified, so it is returned as-is.
GOLDEN_COMMIT_CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "cache" / "rebench_golden_commits.jsonl"

# Retry transient network errors / 5xx with exponential backoff. Rate limiting
# (403/429) is handled separately by waiting for the reset — see ``_http_get`` —
# because its backoff (up to the primary reset, ~1h) is far longer than makes
# sense for the transient-error retry budget.
_RETRY = Retry(
    total=5,
    backoff_factor=1.0,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)
# Waits at least this long get a user-facing notice (short blips stay quiet).
_LONG_WAIT_NOTICE = 30.0
_local = threading.local()

# Dedupe the long-wait notice across concurrent workers that all hit the same
# reset: only the first announces the pause for a given resume time.
_wait_notice_lock = threading.Lock()
_wait_notice_until = 0.0


class GoldenCommitError(str, Enum):
    """Why golden-commit identification failed, one value per verification step."""

    NO_PATCH = "no_patch"  # row has no non-empty ``patch``
    MISSING_FIELDS = "missing_fields"  # ``repo`` / ``base_commit`` / ``instance_id`` absent
    UNPARSEABLE_PR_NUMBER = "unparseable_pr_number"  # instance_id has no trailing ``-<digits>``
    PR_FETCH_FAILED = "pr_fetch_failed"  # GitHub PR request errored or returned malformed JSON
    PR_NOT_FOUND = "pr_not_found"  # HTTP 404: no such PR, or the repo is gone/renamed/private
    PR_NOT_MERGED = "pr_not_merged"  # the PR was never merged
    BASE_COMMIT_MISMATCH = "base_commit_mismatch"  # ``pr.base.sha != base_commit``
    NO_MERGE_COMMIT = "no_merge_commit"  # PR is merged but has no ``merge_commit_sha``
    DIFF_FETCH_FAILED = "diff_fetch_failed"  # both diff requests errored
    PATCH_ID_FAILED = "patch_id_failed"  # ``git patch-id`` could not be computed
    DIFF_MISMATCH = "diff_mismatch"  # patch + test_patch equals neither the landed nor the PR diff


def _build_session(token: str | None) -> requests.Session:
    """Create a keep-alive session with retry/backoff and the auth header baked in."""
    session = requests.Session()
    session.headers["User-Agent"] = "sourceworldbench-benchmarks"
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    adapter = HTTPAdapter(max_retries=_RETRY, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    return session


def _thread_session(token: str | None) -> requests.Session:
    """Return this thread's cached session, rebuilding it if the token changed.

    One session per thread keeps connection reuse simple and side-steps any doubt
    about sharing a single :class:`requests.Session` across threads.
    """
    if getattr(_local, "session", None) is None or getattr(_local, "token", None) != token:
        _local.session = _build_session(token)
        _local.token = token
    return _local.session


def _pr_number_from_instance_id(instance_id: str) -> int | None:
    """Extract the candidate PR number from a rebench ``instance_id``.

    The rebench id has the form ``{owner}__{repo}-{N}`` (e.g.
    ``0b01001001__spectree-64`` → ``64``).  Repo names may contain hyphens, so we
    only trust the final ``-<digits>`` segment.  Returns ``None`` if the tail is
    not a positive integer.
    """
    _, _, tail = instance_id.rpartition("-")
    if not tail.isdigit():
        return None
    number = int(tail)
    return number or None


def _is_rate_limited(response: requests.Response) -> bool:
    """Whether ``response`` is a GitHub rate-limit rejection (primary or secondary)."""
    if response.status_code not in (403, 429):
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":  # primary quota exhausted
        return True
    if "retry-after" in response.headers:  # secondary limit
        return True
    return "rate limit" in response.text.lower()


def _rate_limit_wait(response: requests.Response) -> float:
    """Seconds to wait before retrying a rate-limited ``response``.

    Prefers ``Retry-After`` (secondary limits), then ``X-RateLimit-Reset`` (primary
    quota), falling back to a fixed pause when the server gives no hint.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return float(retry_after)
    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0.0, float(reset) - time.time())
    return 60.0


def _notify_long_wait(wait: float) -> None:
    """Emit a single user-facing notice when a long rate-limit pause begins.

    Short blips stay quiet, and concurrent workers hitting the same reset collapse
    to one line, so a stalled-looking run reads as "pausing", not "stuck".
    """
    global _wait_notice_until
    if wait < _LONG_WAIT_NOTICE:
        return
    resume_at = time.time() + wait
    with _wait_notice_lock:
        if resume_at <= _wait_notice_until + 5.0:  # already announced for this reset window
            return
        _wait_notice_until = resume_at
    logger.warning(
        "GitHub rate limit reached — pausing ~%.0fs (until ~%s) for quota reset",
        wait, time.strftime("%H:%M:%S", time.localtime(resume_at)),
    )


def _http_get(
    url: str, *, session: requests.Session, timeout: int, accept: str | None = None
) -> requests.Response | None:
    """GET ``url`` reusing ``session``, waiting out GitHub rate limits.

    On a rate-limit rejection the call sleeps until the quota resets and retries —
    indefinitely — so a batch pauses instead of failing every remaining item. The
    returned response is therefore never a rate-limit rejection; the caller inspects
    ``status_code`` to tell a 404 from a throttled 5xx. Returns ``None`` only on a
    network error.
    """
    headers = {"Accept": accept} if accept else None
    while True:
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("request failed for %s: %s", url, exc)
            return None
        if not _is_rate_limited(response):
            return response
        wait = _rate_limit_wait(response)
        _notify_long_wait(wait)
        logger.debug("rate limited on %s; waiting %.0fs for quota reset", url, wait)
        time.sleep(wait + 1.0)


def _patch_id(diff_text: str) -> str | None:
    """Return the ``git patch-id --stable`` of a diff, or ``None`` if it cannot be computed.

    ``git patch-id`` reads a diff from stdin and needs no repository.  With
    ``--stable`` the id is independent of line-number offsets and of file/hunk
    ordering, so two diffs with the same id apply the same content change.
    """
    if not diff_text.strip():
        return None
    if not diff_text.endswith("\n"):
        diff_text += "\n"
    try:
        proc = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=diff_text.encode(),
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("git patch-id unavailable: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace").split()
    return out[0] if out else None


@lru_cache(maxsize=None)
def _load_golden_commit_cache(path: str) -> dict[str, str]:
    """Load the ``instance_id`` → golden-commit map from ``path`` (empty if missing).

    Parsed once per path and memoized: every call to :func:`identify_golden_commit`
    reuses the same dict instead of re-reading the file.
    """
    cache_file = Path(path)
    if not cache_file.exists():
        return {}
    cache: dict[str, str] = {}
    with cache_file.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            cache[entry["instance_id"]] = entry["golden_commit"]
    return cache


def identify_golden_commit(
    rebench_dp: dict[str, Any],
    *,
    token: str | None = None,
    timeout: int = 30,
    session: requests.Session | None = None,
    cache_path: str | Path | None = GOLDEN_COMMIT_CACHE_PATH,
) -> str | GoldenCommitError:
    """Identify the guaranteed golden commit SHA of a SWE-rebench datapoint.

    Verification is performed against GitHub (see module docstring): the PR named
    by the ``instance_id`` suffix must be merged, branch from the row's
    ``base_commit``, and its landed diff (or, as a fallback, the PR's own diff) must
    exactly equal the row's ``patch`` plus ``test_patch``.  On success the PR's
    ``merge_commit_sha`` — the commit that landed the fix on the base branch — is
    returned.

    Args:
        rebench_dp: A SWE-rebench datapoint (needs ``instance_id``, ``repo``,
            ``base_commit``, ``patch`` and ``test_patch``).
        token: GitHub token for authenticated requests. Defaults to the
            ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variable. Unauthenticated
            requests are heavily rate-limited.
        timeout: Per-request timeout in seconds.
        session: Optional keep-alive session to reuse. When omitted, a per-thread
            session is used so repeated calls reuse connections.
        cache_path: JSONL file of precomputed golden commits keyed by ``instance_id``
            (see :data:`GOLDEN_COMMIT_CACHE_PATH`). A cache hit is returned
            immediately, skipping all GitHub requests; a miss falls through to the
            live verification. Pass ``None`` to bypass the cache entirely.

    Returns:
        The golden ``merge_commit_sha`` (``str``) on a verified match, or a
        :class:`GoldenCommitError` naming the step that failed. Use
        ``isinstance(result, GoldenCommitError)`` to tell them apart.
    """
    instance_id = str(rebench_dp.get("instance_id") or "")

    if cache_path is not None and instance_id:
        cached = _load_golden_commit_cache(str(cache_path)).get(instance_id)
        if cached is not None:
            return cached

    patch = str(rebench_dp.get("patch") or "")
    if not patch.strip():
        return GoldenCommitError.NO_PATCH

    repo = str(rebench_dp.get("repo") or "")
    base_commit = str(rebench_dp.get("base_commit") or "")
    if not repo or not base_commit or not instance_id:
        return GoldenCommitError.MISSING_FIELDS

    pr_number = _pr_number_from_instance_id(instance_id)
    if pr_number is None:
        logger.warning("could not parse PR number from instance_id %r", instance_id)
        return GoldenCommitError.UNPARSEABLE_PR_NUMBER

    if session is None:
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        session = _thread_session(token)

    # 1. Fetch the candidate PR metadata and anchor it to base_commit.
    pr_resp = _http_get(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        timeout=timeout,
        accept="application/vnd.github+json",
        session=session,
    )
    if pr_resp is None:
        return GoldenCommitError.PR_FETCH_FAILED
    if pr_resp.status_code == 404:
        logger.warning("%s PR #%d not found (repo or PR is gone)", repo, pr_number)
        return GoldenCommitError.PR_NOT_FOUND
    if pr_resp.status_code != 200:
        logger.warning("%s PR #%d metadata returned HTTP %d", repo, pr_number, pr_resp.status_code)
        return GoldenCommitError.PR_FETCH_FAILED
    try:
        pr = pr_resp.json()
    except ValueError:
        return GoldenCommitError.PR_FETCH_FAILED

    if not pr.get("merged"):
        logger.warning("%s PR #%d is not merged", repo, pr_number)
        return GoldenCommitError.PR_NOT_MERGED
    if pr.get("base", {}).get("sha") != base_commit:
        logger.warning(
            "%s PR #%d base sha %s != base_commit %s",
            repo, pr_number, pr.get("base", {}).get("sha"), base_commit,
        )
        return GoldenCommitError.BASE_COMMIT_MISMATCH
    merge_commit_sha = pr.get("merge_commit_sha")
    if not merge_commit_sha:
        return GoldenCommitError.NO_MERGE_COMMIT

    # 2. Require patch + test_patch to *exactly* reproduce the golden change.
    #    Join the two back-to-back, each with exactly one trailing newline so the
    #    next `diff --git` header starts on its own line (a blank line between
    #    sections would corrupt the whole-diff parse).
    parts = [p for p in (patch, str(rebench_dp.get("test_patch") or "")) if p.strip()]
    combined = "".join(p if p.endswith("\n") else p + "\n" for p in parts)
    combined_id = _patch_id(combined)
    if combined_id is None:
        return GoldenCommitError.PATCH_ID_FAILED

    # Two references are accepted: SWE-rebench's stored diff sometimes equals the
    # net change landed on base_commit (compare base...merge) and sometimes the PR's
    # own diff (pulls/{n}.diff) — they diverge with the PR's commit/merge structure.
    # The compare form matches the majority, so try it first; only fall back to the
    # PR diff (a third request) when it does not match.
    diff_urls = (
        f"{GITHUB_API}/repos/{repo}/compare/{base_commit}...{merge_commit_sha}",
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
    )
    fetched_any = False
    for url in diff_urls:
        resp = _http_get(url, timeout=timeout, accept="application/vnd.github.diff", session=session)
        if resp is None or resp.status_code != 200:
            continue
        fetched_any = True
        if _patch_id(resp.content.decode("utf-8", errors="replace")) == combined_id:
            return str(merge_commit_sha)

    if not fetched_any:
        return GoldenCommitError.DIFF_FETCH_FAILED
    logger.warning("%s PR #%d: patch + test_patch matches neither the landed nor the PR diff", repo, pr_number)
    return GoldenCommitError.DIFF_MISMATCH


def identify_golden_commits(
    rebench_dps: Iterable[dict[str, Any]],
    *,
    token: str | None = None,
    timeout: int = 30,
    workers: int = 10,
    progress: bool = True,
    cache_path: str | Path | None = GOLDEN_COMMIT_CACHE_PATH,
) -> list[str | GoldenCommitError]:
    """Identify golden commits for many datapoints concurrently.

    Runs :func:`identify_golden_commit` over a thread pool — the work is
    network-bound, so threads give near-linear speedup up to GitHub's rate limits.
    Keep ``workers`` modest (≈8–10): GitHub's secondary rate limit caps concurrent
    requests, and a rate-limited request pauses for the reset rather than failing.

    Args:
        rebench_dps: SWE-rebench datapoints.
        token: GitHub token; defaults to ``GITHUB_TOKEN`` / ``GH_TOKEN``.
        timeout: Per-request timeout in seconds.
        workers: Maximum number of concurrent worker threads.
        progress: Show a progress bar that advances as each datapoint finishes.
        cache_path: JSONL cache of precomputed golden commits, forwarded to
            :func:`identify_golden_commit`. Pass ``None`` to bypass it.

    Returns:
        Results aligned with the input order — each a golden ``merge_commit_sha``
        (``str``) or a :class:`GoldenCommitError`.
    """
    dps = list(rebench_dps)
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def worker(dp: dict[str, Any]) -> str | GoldenCommitError:
        return identify_golden_commit(dp, token=token, timeout=timeout, cache_path=cache_path)

    # Advance the bar as futures complete (not in submit order); reassemble into the
    # input order via each future's index.
    out: dict[int, str | GoldenCommitError] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(worker, dp): i for i, dp in enumerate(dps)}
        for future in track(
            as_completed(futures), total=len(dps), description="Identifying golden commits", disable=not progress
        ):
            out[futures[future]] = future.result()
    return [out[i] for i in range(len(dps))]
