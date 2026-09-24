# `task_specific_datasets/` — how this folder is developed

## What this folder is

We build a lot of complicated machinery. This folder is the small, readable face of it.

`anonymous-research-730875/sourceworldbench-single-state` is our backend dataset: broad, raw, curated by another team.
Each script here samples one **curated, task-specific dataset** out of it and pushes that to the Hub.
One script, one dataset, one file you can read top to bottom.

**The promise: to know how a published dataset was made you read one file, and you need to know
nothing about the rest of the project to follow it.** Two people rely on it:

- **the teammate adding or reviewing a script** — who needs the shape predictable, so a review is
  about the methodology and not the plumbing;
- **someone from an adjacent team holding a published dataset** — who is looking at their own
  results, wants to check how the rows were chosen, and wants to come back with "this filter is wrong
  for us" or "sample this differently".

Everything below follows from serving both of them with the same file. Rules are numbered and
imperative, and each says *why* it exists. If you are an agent working in this folder: follow the
rules and the checklist literally, and treat **Non-goals** as changes you do not make on your own
initiative.

---

## Rules

### 1. One script per output dataset

- Named after what it builds: `build_<dataset>.py`.
- Never two datasets from one script, and never one dataset from two scripts.
- When a script is superseded, **delete it**. The folder shows only what is currently produced; git
  remembers the rest.

*Why:* the adjacent-team reader arrives holding a dataset name. That name has to lead to exactly one
file, with no branching to trace.

**A brand-new dataset gets its repo created by hand, in the Hugging Face Web UI, first** — owner or
organization, visibility and license decided there, before the first push. Nothing in the code
enforces this, which is why it is written down: `latest_tag` reads a missing repo as "no tags yet"
and `clear_stale_dataset_info` returns early when there is no card, so a first push would quietly
create a repo owned by whoever ran it, with defaults nobody chose.

### 2. A script is independent of the main package

- Never import `sourceworldbench_benchmarks`. A script talks to the Hub, to third-party libraries (`datasets`,
  `huggingface_hub`, the standard library), and to nothing of ours except `dataset_utils` and
  `dataset_filters`.
- Nothing in `src/` or `tests/` may import from `task_specific_datasets/`. The dependency arrow only points out.
- No project CLI entry point, no shared settings module, no `pyproject` script registration.
- A script must still run if it is copied out of the repo with `dataset_utils.py` and
  `dataset_filters.py` beside it — hence the flat `from dataset_utils import ...`, and hence running
  it as a file.

*Why:* the moment a script reaches into the package, it stops being readable on its own and starts
inheriting our complexity. Not architectural purity — it is what keeps the file readable by someone
who has never opened `src/`.

### 3. The source belongs to someone else

`sourceworldbench-single-state` is curated by another team. Treat it as an external input that changes without
telling us.

- **Assume a rerun.** A script must be safe to run again at any time, and a rerun must see the
  current Hub state — hence the forced re-download rather than the local cache.
- **Depend on as little of it as possible.** The column list is the entire contract: one constant
  (`SOURCE_COLUMNS`), visible on the dataset page on HF. Project the source onto it.
- **Never require knowledge of how the source was built.** If understanding a script means first
  understanding the backend pipeline, the script is doing too much.
- A grown column or a changed meaning is fixed by a constant and a comment, not a new layer of code.

### 4. What is shared, and what is not

Two modules own three things between them, and nothing else:

- **`dataset_utils.py`** — (1) *the source contract*: `load_source_dataset` (projected, disk-backed,
  one ref per source row), `StateRef`, `PassedTests`, `load_swefficiency_instance_ids`; (2) *the
  release mechanics*: picking the next tag, the repo-card cleanup a push needs, and the push itself.
- **`dataset_filters.py`** — (3) *the source-level filters*: the `drop_*` functions that leave a
  state out over a property of **the source** (no `swefficiency` block, no reported outcome, a stem
  spanning two containers, an instance the support dataset has never heard of, no local profiling
  report), the `partition` they are all built on, `report_skipped`, and the two group helpers they
  share with the samplers.

A filter decides nothing on a build's behalf: it hands back `(kept, skipped)`, and **which filters a
build applies, and in which order, stays one readable list in its `main`** — the list a reader checks
to see everything a dataset left out.

Everything downstream stays in the script: which states this task wants, how rows are sampled, what a
row holds — **even when two scripts look alike**.

The test for the seam:

- "Would every future script want this, unchanged, without knowing what task it serves?" → shared.
- "Does this express what *this* dataset asks?" → the script, always.

When in doubt leave it in the script. Moving code into a shared module later is cheap; pulling a
task's opinion back out after four scripts depend on it is not.

> **How the filters got there.** Every build used to carry its own copy. They moved as they were —
> same bodies, same `(kept, skipped)` shape, same order in every `main` — with two generalized on the
> way, one shared module having no room for four names for one function: `drop_groups_without_base`
> became `drop_groups_without_variant` (the variant is an argument now), and the twin
> `instance_id`-in-a-local-report filters, `drop_untimed_states` and `drop_unprofiled_states`, became
> `drop_states_without_profile_report`. A filter that encodes a task's own requirement, rather than a
> quirk of the source, still belongs in the script.

### 5. Constants at the top, `main()` takes nothing

- Every knob is a module-level constant with a comment saying what it means and why it is that value.
- No `argparse`, no environment variables, no config file, no defaults hidden in a signature.
- The one interaction is the release-tag prompt, asked **before** any expensive work, so a rejected
  answer costs a prompt instead of a full rebuild and push.
- Changing a build means editing the file, and that edit is the record of the change.

*Why:* a reader auditing methodology should see the actual numbers that produced the dataset, not a
flag they have to reconstruct from a shell history.

### 6. Docstrings are a helper, not a burden

A module docstring answers what a reader shows up with — what does this dataset ask? what goes in and
comes out? which states are used, and why are some left out? how are rows and tests chosen? what does
one row hold? The four current scripts answer in the same shape:

| Block | Holds |
| --- | --- |
| `Asks:` | the question a row poses, in a sentence |
| `in` / `out` | repo ids and splits, two aligned lines |
| `TEMPORARY: …` | only where the answer comes from outside the source dataset |
| `Which states are used` | one line per filter: its name, and the reason it drops |
| `Which rows are written` | the sampling, as a few labelled lines |
| `What a row holds` | one line per column, nullability included |

**No required section list and no template** — that is the shape that fits these four, not a form to
fill in. Length has to be earned: a paragraph is worth writing when it saves the reader from reading
code, and is a liability when it merely restates it.

- Prefer a column of short labelled lines to a paragraph; prefer a paragraph to nothing.
- Explain a decision **next to the code that makes it**, not three hundred lines away where it will
  drift.
- Write down the *why*. The *what* is already in the code.
- A `TODO` says what is undecided **and** what happens meanwhile — see `drop_container_conflicts` for
  the shape.
- When prose and a constant disagree, the constant is the truth and the prose is a bug. Fix the
  prose.

### 7. A rebuild of an unchanged source produces the same rows

- All randomness goes through one seeded `random.Random`, created from a constant seed.
- No wall-clock, no process-dependent ordering. Sets are sorted before they are drawn from, and
  sampled output is re-sorted by source row index.
- Source order is preserved end to end, so two runs line up row for row.
- Everything a generated dataset depends on goes into `gen_kwargs`, so a changed input can never be
  answered out of a previous run's cache.

*Why:* the adjacent-team reader's suggestion becomes "rerun it and diff". That only works if a rerun
is a fair comparison.

### 8. Nothing arbitrarily large is held in Python

- The source stays a projected, disk-backed `Dataset`.
- States are held as scalar refs, never as rows.
- A group's big lists are read one state at a time.
- Output rows are written one at a time.

*Why:* a single row here can already carry thousands of test names. A build that fits in memory today
is not a build that fits after the backend dataset doubles.

### 9. Say out loud what was left out

- Every filter reports what it dropped and why — one line per state, or a count where a filter covers
  most of the source and a line each would bury the rest of the output.
- Before the push, the build prints what it kept: rows, contributing groups, label distribution.
- A silent drop is a bug. "Why is my instance not in here?" is the adjacent team's most common
  question, and the answer should already be in the run's output.

### 10. Fail where the assumption breaks

- A contradiction in the source raises instead of being papered over — a test reported as both passed
  and failed is a real problem and should stop the build.
- Where an earlier filter guarantees an invariant, assert it at the point of use and name that filter
  in the comment, so a reordering fails loudly instead of producing quiet nonsense.

---

## The shape every script has

Read in this order, and write in this order:

| # | Section | What lives there |
| --- | --- | --- |
| 1 | Module docstring | The dataset: what it asks, in, out, which states, which rows, what a row holds |
| 2 | Imports | Standard library, `dataset_filters`, `dataset_utils`, third-party. Nothing from `src/` |
| 3 | Constants | Source and target repo ids and splits, the source column contract, the output `Features`, the prompts and label glossary, the sampling knobs and seed |
| 4 | Type aliases | The two or three shapes the sampling code passes around, each with a one-line comment |
| 5 | Eligible source states | Only a filter this task needs that `dataset_filters` does not have. The shared ones are not repeated — they are composed, one per reason, in `main` |
| 6 | Sampling | The task's actual opinion: which rows, which tests, with what weights |
| 7 | Output rows | One source row plus a draw becomes one output row |
| 8 | Build and push | Load, generate the dataset, print the summary, push and tag |
| 9 | `main()` | The whole story in twenty lines: ask for the tag, load, filter, sample, build, summarize, push |

A reader who only reads the docstring and `main()` should come away with a correct picture. If they
would not, the fix is in the file, not in this document.

## Checklist for a new script, and for reviewing one

- [ ] Name is `build_<dataset>.py` and matches the output repo id constant.
- [ ] For a brand-new dataset: the repo was created in the HF Web UI first — owner, visibility and
      license set there — not implicitly by the first push.
- [ ] Superseded script, if any, is deleted in the same change.
- [ ] No import from `sourceworldbench_benchmarks`; nothing outside `task_specific_datasets/` imports this file.
- [ ] Source columns are declared as one constant, and the source is opened through
      `load_source_dataset`, which projects onto it and force re-downloads, so a rerun sees the
      current Hub state.
- [ ] Everything reusable goes through `dataset_utils` or `dataset_filters`; nothing task-specific was
      pushed into either, and the filters a build applies are still listed in its `main`.
- [ ] Every knob is a top-level constant with a comment; `main()` takes no arguments.
- [ ] The release tag is asked for before any expensive work.
- [ ] Randomness comes from one seeded `random.Random`; two runs on an unchanged source agree.
- [ ] Nothing unbounded is materialized in Python; output rows are written one at a time.
- [ ] Every filter reports what it dropped and why; the build prints a summary before pushing.
- [ ] The docstring and the constants tell the same story.
- [ ] Output `Features` are explicit, in output order, with a comment on any column that can be null.

## Non-goals

Deliberate absences. Do not add these without a conversation:

- **A CLI.** No `argparse`, no `typer`, no `--dry-run`. Constants are the interface.
- **A framework.** No shared base class, no plugin registry, no config-driven builder producing
  several datasets. The repetition between scripts is the price of each one being readable alone, and
  we are choosing to pay it.
- **A parameterized mega-script.** A new variant of a task is a new file, not a new flag.
- **Coupling to the package.** If a script needs something `src/` already does, copy the small part it
  needs, or reconsider whether the script is the right place for it.
- **Knowledge of the backend pipeline.** How `sourceworldbench-single-state` came to be is not this folder's
  business.
