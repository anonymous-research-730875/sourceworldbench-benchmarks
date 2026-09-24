"""Tests for golden-commit identification (``swerebench.golden_commit``).

Network is never touched: :func:`identify_golden_commit` accepts an explicit
``session``, so every HTTP-dependent test drives it with an in-memory fake session
that returns canned responses keyed by (url, accept) — letting us exercise every
verification step and every :class:`GoldenCommitError` branch deterministically.
"""

import json
import time
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.swerebench import golden_commit as gc
from sourceworldbench_benchmarks.swerebench.golden_commit import (
    GoldenCommitError,
    _build_session,
    _is_rate_limited,
    _load_golden_commit_cache,
    _notify_long_wait,
    _patch_id,
    _pr_number_from_instance_id,
    _rate_limit_wait,
    identify_golden_commit,
    identify_golden_commits,
)

# A valid unified diff and a matching "test" diff. Their exact bytes are what a
# verified GitHub compare/PR diff must reproduce for identification to succeed.
FIX_DIFF = "diff --git a/pkg/x.py b/pkg/x.py\n--- a/pkg/x.py\n+++ b/pkg/x.py\n@@ -1 +1 @@\n-old\n+new\n"
TEST_DIFF = (
    "diff --git a/tests/test_x.py b/tests/test_x.py\n"
    "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-assert 0\n+assert 1\n"
)
BASE_SHA = "a" * 40
MERGE_SHA = "b" * 40


def _rebench_dp(**overrides) -> dict:
    """A rebench datapoint whose patch+test_patch reproduce ``COMBINED_DIFF``."""
    dp = {
        "instance_id": "owner__repo-42",
        "repo": "owner/repo",
        "base_commit": BASE_SHA,
        "patch": FIX_DIFF,
        "test_patch": TEST_DIFF,
    }
    dp.update(overrides)
    return dp


# The two patches joined exactly as identify_golden_commit joins them: each part
# gets exactly one trailing newline. Both already end in "\n", so this is a plain
# concatenation — the reference diff a verified compare/PR response must equal.
COMBINED_DIFF = FIX_DIFF + TEST_DIFF


class FakeResponse:
    def __init__(self, status_code=200, *, json_data=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        # `text` is consulted by rate-limit detection; default to the body text.
        self.text = text or (content.decode("utf-8", errors="replace") if content else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Maps ``(url, accept-header)`` to a canned response, recording every call.

    A ``None`` value in the map is returned as-is only by ``_http_get`` semantics
    via raising :class:`requests.RequestException`; to model a network error use
    :class:`RaisingSession` instead.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, headers=None, timeout=None):
        accept = (headers or {}).get("Accept")
        self.calls.append((url, accept))
        # Try the (url, accept) key first, then fall back to url-only.
        if (url, accept) in self.responses:
            return self.responses[(url, accept)]
        return self.responses[url]


# --------------------------------------------------------------------------- #
# _pr_number_from_instance_id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("instance_id", "expected"),
    [
        ("0b01001001__spectree-64", 64),
        ("owner__repo-42", 42),
        ("owner__repo-with-hyphens-123", 123),  # repo names may contain hyphens
        ("owner__repo", None),  # no trailing -<digits>
        ("owner__repo-abc", None),  # tail not numeric
        ("owner__repo-0", None),  # 0 is not a valid PR number
        ("owner__repo-", None),  # empty tail
    ],
)
def test_pr_number_from_instance_id(instance_id, expected):
    assert _pr_number_from_instance_id(instance_id) == expected


# --------------------------------------------------------------------------- #
# _is_rate_limited
# --------------------------------------------------------------------------- #


def test_is_rate_limited_ok_status():
    assert _is_rate_limited(FakeResponse(200)) is False


def test_is_rate_limited_primary_quota_exhausted():
    resp = FakeResponse(403, headers={"x-ratelimit-remaining": "0"})
    assert _is_rate_limited(resp) is True


def test_is_rate_limited_secondary_retry_after():
    resp = FakeResponse(429, headers={"retry-after": "10"})
    assert _is_rate_limited(resp) is True


def test_is_rate_limited_message_in_body():
    resp = FakeResponse(403, text="You have exceeded a secondary rate limit")
    assert _is_rate_limited(resp) is True


def test_is_rate_limited_plain_403_is_not_rate_limit():
    # A 403 with quota remaining and no rate-limit hint (e.g. a genuine forbidden).
    resp = FakeResponse(403, headers={"x-ratelimit-remaining": "42"}, text="Forbidden")
    assert _is_rate_limited(resp) is False


# --------------------------------------------------------------------------- #
# _rate_limit_wait
# --------------------------------------------------------------------------- #


def test_rate_limit_wait_prefers_retry_after():
    resp = FakeResponse(429, headers={"retry-after": "7", "x-ratelimit-reset": "9999999999"})
    assert _rate_limit_wait(resp) == 7.0


def test_rate_limit_wait_uses_reset_header():
    resume = time.time() + 50
    resp = FakeResponse(403, headers={"x-ratelimit-reset": str(int(resume))})
    wait = _rate_limit_wait(resp)
    assert 40 <= wait <= 60


def test_rate_limit_wait_reset_in_past_is_clamped_to_zero():
    resp = FakeResponse(403, headers={"x-ratelimit-reset": "1"})  # long past
    assert _rate_limit_wait(resp) == 0.0


def test_rate_limit_wait_default_when_no_hints():
    assert _rate_limit_wait(FakeResponse(403)) == 60.0


# --------------------------------------------------------------------------- #
# _patch_id
# --------------------------------------------------------------------------- #


def test_patch_id_empty_returns_none():
    assert _patch_id("") is None
    assert _patch_id("   \n  ") is None


def test_patch_id_stable_across_line_offsets():
    # --stable makes the id independent of line-number offsets.
    a = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    b = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -100 +100 @@\n-old\n+new\n"
    id_a = _patch_id(a)
    assert id_a is not None
    assert id_a == _patch_id(b)


def test_patch_id_differs_for_different_content():
    a = _patch_id("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    b = _patch_id("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+other\n")
    assert a != b


def test_patch_id_missing_trailing_newline_is_handled():
    with_nl = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    without_nl = with_nl.rstrip("\n")
    assert _patch_id(with_nl) == _patch_id(without_nl)


# --------------------------------------------------------------------------- #
# _load_golden_commit_cache
# --------------------------------------------------------------------------- #


def test_load_cache_missing_file_returns_empty(tmp_path: Path):
    assert _load_golden_commit_cache(str(tmp_path / "nope.jsonl")) == {}


def test_load_cache_parses_entries_and_skips_blank_lines(tmp_path: Path):
    path = tmp_path / "cache.jsonl"
    path.write_text(
        json.dumps({"instance_id": "a__b-1", "golden_commit": "c" * 40})
        + "\n\n"  # blank line in the middle
        + json.dumps({"instance_id": "a__b-2", "golden_commit": "d" * 40})
        + "\n",
        encoding="utf-8",
    )
    cache = _load_golden_commit_cache(str(path))
    assert cache == {"a__b-1": "c" * 40, "a__b-2": "d" * 40}


def test_load_cache_is_memoized_per_path(tmp_path: Path):
    path = tmp_path / "cache.jsonl"
    path.write_text(json.dumps({"instance_id": "a__b-1", "golden_commit": "c" * 40}) + "\n", encoding="utf-8")
    first = _load_golden_commit_cache(str(path))
    # Mutating the file afterwards must not change the memoized result.
    path.write_text(json.dumps({"instance_id": "x__y-9", "golden_commit": "e" * 40}) + "\n", encoding="utf-8")
    assert _load_golden_commit_cache(str(path)) is first


# --------------------------------------------------------------------------- #
# _build_session
# --------------------------------------------------------------------------- #


def test_build_session_sets_auth_header_when_token_given():
    session = _build_session("tok123")
    assert session.headers["Authorization"] == "Bearer tok123"
    assert session.headers["User-Agent"] == "sourceworldbench-benchmarks"


def test_build_session_without_token_has_no_auth_header():
    session = _build_session(None)
    assert "Authorization" not in session.headers


# --------------------------------------------------------------------------- #
# _notify_long_wait
# --------------------------------------------------------------------------- #


def test_notify_long_wait_stays_quiet_for_short_waits(caplog):
    gc._wait_notice_until = 0.0
    with caplog.at_level("WARNING", logger=gc.logger.name):
        _notify_long_wait(1.0)
    assert caplog.records == []


def test_notify_long_wait_announces_once_per_reset_window(caplog):
    gc._wait_notice_until = 0.0
    with caplog.at_level("WARNING", logger=gc.logger.name):
        _notify_long_wait(120.0)
        _notify_long_wait(120.0)  # same window: deduped, no second line
    warnings = [r for r in caplog.records if "rate limit" in r.getMessage().lower()]
    assert len(warnings) == 1


# --------------------------------------------------------------------------- #
# identify_golden_commit: cache + validation short-circuits (no session needed)
# --------------------------------------------------------------------------- #


def _cache_file(tmp_path: Path, mapping: dict) -> Path:
    path = tmp_path / "golden.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for iid, sha in mapping.items():
            fh.write(json.dumps({"instance_id": iid, "golden_commit": sha}) + "\n")
    return path


def test_cache_hit_returns_immediately_without_session(tmp_path: Path):
    cache = _cache_file(tmp_path, {"owner__repo-42": MERGE_SHA})
    # No session passed: a network attempt would raise, so returning proves the
    # cache short-circuits before any HTTP work.
    result = identify_golden_commit(_rebench_dp(), cache_path=cache)
    assert result == MERGE_SHA


def test_cache_bypassed_when_none(tmp_path: Path):
    # With cache_path=None and a row lacking a patch, we fall straight to NO_PATCH.
    result = identify_golden_commit(_rebench_dp(patch=""), cache_path=None)
    assert result is GoldenCommitError.NO_PATCH


def test_no_patch(tmp_path: Path):
    assert identify_golden_commit(_rebench_dp(patch="   "), cache_path=None) is GoldenCommitError.NO_PATCH


@pytest.mark.parametrize("missing", ["repo", "base_commit", "instance_id"])
def test_missing_fields(missing):
    assert identify_golden_commit(_rebench_dp(**{missing: ""}), cache_path=None) is GoldenCommitError.MISSING_FIELDS


def test_unparseable_pr_number():
    dp = _rebench_dp(instance_id="owner__repo")
    assert identify_golden_commit(dp, cache_path=None) is GoldenCommitError.UNPARSEABLE_PR_NUMBER


# --------------------------------------------------------------------------- #
# identify_golden_commit: GitHub-dependent verification steps
# --------------------------------------------------------------------------- #

PR_META_URL = "https://api.github.com/repos/owner/repo/pulls/42"
COMPARE_URL = f"https://api.github.com/repos/owner/repo/compare/{BASE_SHA}...{MERGE_SHA}"


def _pr_meta(**over) -> dict:
    meta = {"merged": True, "base": {"sha": BASE_SHA}, "merge_commit_sha": MERGE_SHA}
    meta.update(over)
    return meta


def test_pr_not_found_404():
    session = FakeSession({PR_META_URL: FakeResponse(404)})
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.PR_NOT_FOUND


def test_pr_fetch_non_200_non_404():
    session = FakeSession({PR_META_URL: FakeResponse(500)})
    # 500 is retried by the adapter in real use; here the fake returns it directly,
    # and identify treats any non-200/404 metadata status as a fetch failure.
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.PR_FETCH_FAILED


def test_pr_fetch_malformed_json():
    session = FakeSession({PR_META_URL: FakeResponse(200, json_data=None)})
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.PR_FETCH_FAILED


def test_pr_not_merged():
    session = FakeSession({PR_META_URL: FakeResponse(200, json_data=_pr_meta(merged=False))})
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.PR_NOT_MERGED


def test_base_commit_mismatch():
    session = FakeSession({PR_META_URL: FakeResponse(200, json_data=_pr_meta(base={"sha": "f" * 40}))})
    assert (
        identify_golden_commit(_rebench_dp(), session=session, cache_path=None)
        is GoldenCommitError.BASE_COMMIT_MISMATCH
    )


def test_no_merge_commit_sha():
    session = FakeSession({PR_META_URL: FakeResponse(200, json_data=_pr_meta(merge_commit_sha=None))})
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.NO_MERGE_COMMIT


def test_success_via_compare_diff():
    session = FakeSession(
        {
            PR_META_URL: FakeResponse(200, json_data=_pr_meta()),
            COMPARE_URL: FakeResponse(200, content=COMBINED_DIFF.encode()),
        }
    )
    result = identify_golden_commit(_rebench_dp(), session=session, cache_path=None)
    assert result == MERGE_SHA
    # The compare diff matched, so the PR-diff fallback request is never made.
    assert not any(url == PR_META_URL and accept == "application/vnd.github.diff" for url, accept in session.calls)


def test_success_via_pr_diff_fallback():
    # Compare diff does not match; the PR .diff (fetched from the metadata URL with
    # a diff Accept header) does — identification still succeeds.
    session = FakeSession(
        {
            (PR_META_URL, "application/vnd.github+json"): FakeResponse(200, json_data=_pr_meta()),
            (COMPARE_URL, "application/vnd.github.diff"): FakeResponse(200, content=b"diff --git a/z b/z\n"),
            (PR_META_URL, "application/vnd.github.diff"): FakeResponse(200, content=COMBINED_DIFF.encode()),
        }
    )
    result = identify_golden_commit(_rebench_dp(), session=session, cache_path=None)
    assert result == MERGE_SHA


def test_diff_mismatch():
    # Both diff sources are fetched successfully but neither matches patch+test_patch.
    compare_diff = b"diff --git a/z b/z\n@@ -1 +1 @@\n-a\n+b\n"
    pr_diff = b"diff --git a/q b/q\n@@ -1 +1 @@\n-c\n+d\n"
    session = FakeSession(
        {
            (PR_META_URL, "application/vnd.github+json"): FakeResponse(200, json_data=_pr_meta()),
            (COMPARE_URL, "application/vnd.github.diff"): FakeResponse(200, content=compare_diff),
            (PR_META_URL, "application/vnd.github.diff"): FakeResponse(200, content=pr_diff),
        }
    )
    assert identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.DIFF_MISMATCH


def test_diff_fetch_failed_when_both_diff_requests_error():
    session = FakeSession(
        {
            (PR_META_URL, "application/vnd.github+json"): FakeResponse(200, json_data=_pr_meta()),
            (COMPARE_URL, "application/vnd.github.diff"): FakeResponse(500),
            (PR_META_URL, "application/vnd.github.diff"): FakeResponse(500),
        }
    )
    assert (
        identify_golden_commit(_rebench_dp(), session=session, cache_path=None) is GoldenCommitError.DIFF_FETCH_FAILED
    )


def test_patch_only_row_matches_when_diff_is_fix_only():
    # A row with no test_patch: the combined diff is just the fix, so a compare diff
    # equal to the fix alone verifies.
    session = FakeSession(
        {
            PR_META_URL: FakeResponse(200, json_data=_pr_meta()),
            COMPARE_URL: FakeResponse(200, content=FIX_DIFF.encode()),
        }
    )
    dp = _rebench_dp(test_patch="")
    assert identify_golden_commit(dp, session=session, cache_path=None) == MERGE_SHA


# --------------------------------------------------------------------------- #
# identify_golden_commits (batch)
# --------------------------------------------------------------------------- #


def test_identify_golden_commits_preserves_input_order(tmp_path: Path):
    # All served from cache so the batch touches no network; assert order alignment.
    cache = _cache_file(
        tmp_path,
        {"owner__repo-1": "1" * 40, "owner__repo-2": "2" * 40, "owner__repo-3": "3" * 40},
    )
    dps = [
        _rebench_dp(instance_id="owner__repo-3"),
        _rebench_dp(instance_id="owner__repo-1"),
        _rebench_dp(instance_id="owner__repo-2"),
    ]
    results = identify_golden_commits(dps, cache_path=cache, workers=3, progress=False)
    assert results == ["3" * 40, "1" * 40, "2" * 40]


def test_identify_golden_commits_mixes_hits_and_errors(tmp_path: Path):
    cache = _cache_file(tmp_path, {"owner__repo-1": "1" * 40})
    dps = [
        _rebench_dp(instance_id="owner__repo-1"),  # cache hit
        _rebench_dp(instance_id="owner__repo-2", patch=""),  # NO_PATCH (miss, no network)
    ]
    results = identify_golden_commits(dps, cache_path=cache, workers=2, progress=False)
    assert results == ["1" * 40, GoldenCommitError.NO_PATCH]


def test_identify_golden_commits_empty_input(tmp_path: Path):
    assert identify_golden_commits([], cache_path=None, progress=False) == []
