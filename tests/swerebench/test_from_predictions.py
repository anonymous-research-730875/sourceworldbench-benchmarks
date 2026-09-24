"""Tests for prediction-patch repair (``swerebench.from_predictions``).

These two functions are the only thing standing between the dataset and a class of
row that can never be collected: a swefficiency base image carries an untracked
stray file named after the base commit's date, and OpenHands' ``git add -A``
sweeps it into every prediction diff. ``git apply`` then rejects the whole patch
with ``already exists in working directory``.

63 such rows reached the published dataset before this repair existed. Nothing
covered it afterwards, so these tests pin both halves of the contract: a stray
entry alongside real edits is dropped and the edits survive, and a patch that is
*nothing but* the stray entry is refused outright rather than emitted as a row.
"""

from sourceworldbench_benchmarks.swerebench.from_predictions import keep_row, normalize_model_patch

# The exact shape observed in the dataset: an empty file whose name is the base
# commit's timestamp, and nothing else.
STRAY = (
    "diff --git a/2020-12-21 11:17:36 -0600 b/2020-12-21 11:17:36 -0600\n"
    "new file mode 100644\n"
    "index 000000000..e69de29bb\n"
)

REAL = (
    "diff --git a/pandas/core/common.py b/pandas/core/common.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/pandas/core/common.py\n"
    "+++ b/pandas/core/common.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)


def test_stray_timestamp_entry_is_dropped_and_real_edits_survive():
    assert normalize_model_patch(STRAY + REAL) == REAL


def test_stray_entry_is_dropped_wherever_it_appears():
    assert normalize_model_patch(REAL + STRAY) == REAL


def test_patch_of_only_a_stray_entry_normalizes_to_nothing():
    assert normalize_model_patch(STRAY) == ""


def test_missing_final_newline_is_restored():
    assert normalize_model_patch(REAL.rstrip("\n")) == REAL


def test_ordinary_patch_is_untouched():
    assert normalize_model_patch(REAL) == REAL


def test_row_whose_patch_is_only_a_stray_entry_is_refused():
    # The regression that produced 63 uncollectable rows: without this, a patch
    # that carries no model edit at all is emitted as a datapoint.
    keep, reason = keep_row({"model_patch": STRAY})
    assert keep is False
    assert reason == "empty model_patch"


def test_row_with_real_edits_is_kept_despite_a_stray_entry():
    keep, reason = keep_row({"model_patch": STRAY + REAL})
    assert keep is True
    assert reason is None


def test_errored_run_is_refused():
    keep, reason = keep_row({"status": "error", "model_patch": REAL})
    assert keep is False
    assert reason == "status=error"
