# `task_specific_datasets/`

`anonymous-research-730875/sourceworldbench-single-state` is our backend dataset: broad, raw, curated by another team.
Each script here samples one curated, task-specific dataset out of it and pushes that to the Hub.

**One script builds one dataset, and it is readable on its own** — you do not need to know anything
about the rest of the project to follow how a published dataset was made.

## What is here

| Output dataset | Built by | What a row asks |
| --- | --- | --- |
| [`sourceworldbench-single-test-status`](https://huggingface.co/datasets/anonymous-research-730875/sourceworldbench-single-test-status) | `build_single_test_status.py` | one repo state and **one** test name: how does that test behave? |
| [`sourceworldbench-test-status-anchored`](https://huggingface.co/datasets/anonymous-research-730875/sourceworldbench-test-status-anchored) | `build_test_status_anchored.py` | one repo state and a **list** of test names: how does each behave? |
| [`sourceworldbench-time-prediction`](https://huggingface.co/datasets/anonymous-research-730875/sourceworldbench-time-prediction) | `build_time_prediction.py` | one repo state and its **workload command**: how long does it take? |
| [`sourceworldbench-time-hotspots`](https://huggingface.co/datasets/anonymous-research-730875/sourceworldbench-time-hotspots) | `build_time_hotspot.py` | one repo state and its **workload command**: which functions are the hotspots? |

How each chooses its rows:

- **`sourceworldbench-single-test-status`** — at most 2 rows per swefficiency instance, both asking about the same
  test. The test is drawn from the ones the instance's `base` state passes, preferring one the
  instance's states disagree about; the rows then prefer one state that passes it and one that does
  not. Each row also carries what `base` and `fixed` report for that test.
- **`sourceworldbench-test-status-anchored`** — at most 2 rows per instance, at most 100 tests per row, drawn from
  the tests `base` passes and weighted towards the ones more of the instance's states fail.
- **`sourceworldbench-time-prediction`** — every profiled state becomes a row. `Q-seconds` asks for the wall-clock
  seconds outright, `Q-bucket` offers four buckets drawn per row off a ladder of round seconds, and
  three `Q-budget-*` ask whether the workload fits inside 5s, 60s or 300s.
- **`sourceworldbench-time-hotspots`** — every profiled state becomes a row. `Q-top1`, `Q-top5` and `Q-top20` are
  each answered by a prefix of one ranking by exclusive time; the row also carries the whole pool
  that ranking came out of.

Two things to know before using any of them:

- **The test-status datasets only ever ask about tests that pass at base.** A test the unpatched
  `base` state does not pass says nothing about what a state's own changes did to it, so it is never
  a candidate, and an instance with no `base` state contributes no rows at all. Those two builds also
  read `swefficiency/swefficiency`, for the set of instance ids that exist.
- **The time datasets answer with something the backend dataset does not carry.** `sourceworldbench-single-state`
  records neither timing nor profiles, so both builds read their ground truth off local profiling
  reports under `reports/workloads` — one directory per state, named after its `instance_id`. So
  those builds only run on a machine holding those reports, and every unprofiled state is left out.
  Both facts go away once the profiling data lands in the source dataset.

Two shared modules, neither of them a build script:

- **`dataset_utils.py`** — the source contract (how the backend dataset is loaded, addressed and read
  back), the two temporary readers for those profiling reports, and the release mechanics every push
  goes through.
- **`dataset_filters.py`** — the `drop_*` filters the builds share: no swefficiency metadata, a stem
  spanning two containers, an instance the supporting dataset has never heard of, and so on, each
  handing back what it kept and what it dropped. Which of them a build applies, and in which order,
  it does not decide — that stays one readable list in the build script, in `main`, and it is what
  says why a state is missing from a published dataset.

## Where the details are

**Each script's module docstring is the spec of its dataset.** It is the one place that says which
states were used, which were dropped and why, how rows and tests were drawn, and what every column
holds. Open the file named in the table above and read the top of it.

[`DEVELOPMENT.md`](DEVELOPMENT.md) has the rules a script here follows, and how to add one.

## Running one

```sh
python task_specific_datasets/build_single_test_status.py
```

- **Run it as a file**, from anywhere — not as `python -m task_specific_datasets.…`. The task_specific_datasets import their
  shared helpers flatly, as `from dataset_utils import ...`, and running a file puts that file's own
  directory on the import path.
- **Every knob is a constant** at the top of the file. There are no command line arguments: to change
  a build, edit it.
- **A run pushes to the Hub.** It asks which version to tag first, before doing any work, and pushing
  untagged is one of the answers.
- **The dataset repo has to exist already.** A brand-new one is created by hand in the HF Web UI, so
  its owner, visibility and license are a deliberate choice.

## Questions about a dataset

If a result looks wrong, or an instance you expected is missing, the run's own output usually answers
it: each filter prints every state it dropped and why. Bring the instance id and what you expected —
how rows are sampled is a small edit to one build script, and a filter is an edit to
`dataset_filters.py`, and so to every build that applies it.
