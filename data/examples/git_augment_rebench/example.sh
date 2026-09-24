#!/usr/bin/env bash
# =============================================================
# 0. Derive the base datapoints: run the 3 rebench instances in
#    parallel (background). Each appends its base + trajectory
#    states to integration/<rebench-id>.states.jsonl.
# =============================================================
mkdir -p data/examples/git_augment_rebench/integration

uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --rebench-id mit-ll-responsible-ai__hydra-zen-73 \
  --out data/examples/git_augment_rebench/integration/mit-ll-responsible-ai__hydra-zen-73.jsonl \
  --states-out data/examples/git_augment_rebench/integration/mit-ll-responsible-ai__hydra-zen-73.states.jsonl \
  --continue-on-error > data/examples/git_augment_rebench/integration/mit-ll-responsible-ai__hydra-zen-73.log 2>&1 &

uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --rebench-id CORE-GATECH-GROUP__serpent-tools-447 \
  --out data/examples/git_augment_rebench/integration/CORE-GATECH-GROUP__serpent-tools-447.jsonl \
  --states-out data/examples/git_augment_rebench/integration/CORE-GATECH-GROUP__serpent-tools-447.states.jsonl \
  --continue-on-error > data/examples/git_augment_rebench/integration/CORE-GATECH-GROUP__serpent-tools-447.log 2>&1 &

uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --rebench-id 12rambau__sepal_ui-411 \
  --out data/examples/git_augment_rebench/integration/12rambau__sepal_ui-411.jsonl \
  --states-out data/examples/git_augment_rebench/integration/12rambau__sepal_ui-411.states.jsonl \
  --continue-on-error > data/examples/git_augment_rebench/integration/12rambau__sepal_ui-411.log 2>&1 &


# =============================================================
# A. Wait for the 3 background derive commands to finish, then
#    merge their state files into one base_states.jsonl.
#    The states files also contain trajectory-augmentation states,
#    so keep only rows whose instance_id ends in "__base".
# =============================================================
wait

jq -c 'select(.instance_id | endswith("__base"))' \
    data/examples/git_augment_rebench/integration/mit-ll-responsible-ai__hydra-zen-73.states.jsonl \
    data/examples/git_augment_rebench/integration/CORE-GATECH-GROUP__serpent-tools-447.states.jsonl \
    data/examples/git_augment_rebench/integration/12rambau__sepal_ui-411.states.jsonl \
    > data/examples/git_augment_rebench/base_states.jsonl


# =============================================================
# B. Build one git-history report per repo (branch origin/main)
# =============================================================
uv run sourceworldbench-benchmarks build-git-history --base-instance-id hydra-zen__f49084dd__base \
  --in data/examples/git_augment_rebench/base_states.jsonl --branch origin/main \
  --out data/examples/git_augment_rebench/git_history/hydra-zen__main.json

uv run sourceworldbench-benchmarks build-git-history --base-instance-id serpent-tools__f7eb3e1c__base \
  --in data/examples/git_augment_rebench/base_states.jsonl --branch origin/main \
  --out data/examples/git_augment_rebench/git_history/serpent-tools__main.json

uv run sourceworldbench-benchmarks build-git-history --base-instance-id sepal_ui__179bd8d0__base \
  --in data/examples/git_augment_rebench/base_states.jsonl --branch origin/main \
  --out data/examples/git_augment_rebench/git_history/sepal_ui__main.json


# =============================================================
# C. Build the 24 augmented partial rows into git_augmented.partial.jsonl
#    (suffix: p = +step, m = -step, since '+' is not a legal id char)
#    NOTE: the FIRST command reads base_states.jsonl;
#          every command after it reads git_augmented.partial.jsonl.
# =============================================================

# ---- hydra-zen__f49084dd__base : steps -9 -3 3 9 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__f49084dd__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step -9 --suffix githist-m9 \
  --in data/examples/git_augment_rebench/base_states.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__f49084dd__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step -3 --suffix githist-m3 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__f49084dd__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step 3 --suffix githist-p3 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__f49084dd__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step 9 --suffix githist-p9 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push

# ---- hydra-zen__5c240019__base : steps -6 -5 2 7 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__5c240019__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step -6 --suffix githist-m6 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__5c240019__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step -5 --suffix githist-m5 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__5c240019__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step 2 --suffix githist-p2 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id hydra-zen__5c240019__base \
  --report data/examples/git_augment_rebench/git_history/hydra-zen__main.json --step 7 --suffix githist-p7 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push

# ---- serpent-tools__f7eb3e1c__base : steps -9 -5 3 6 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__f7eb3e1c__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step -9 --suffix githist-m9 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__f7eb3e1c__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step -5 --suffix githist-m5 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__f7eb3e1c__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step 3 --suffix githist-p3 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__f7eb3e1c__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step 6 --suffix githist-p6 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push

# ---- serpent-tools__857b9f51__base : steps -8 -3 9 10 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__857b9f51__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step -8 --suffix githist-m8 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__857b9f51__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step -3 --suffix githist-m3 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__857b9f51__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step 9 --suffix githist-p9 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id serpent-tools__857b9f51__base \
  --report data/examples/git_augment_rebench/git_history/serpent-tools__main.json --step 10 --suffix githist-p10 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push

# ---- sepal_ui__179bd8d0__base : steps -10 -8 5 10 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__179bd8d0__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step -10 --suffix githist-m10 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__179bd8d0__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step -8 --suffix githist-m8 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__179bd8d0__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step 5 --suffix githist-p5 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__179bd8d0__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step 10 --suffix githist-p10 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push

# ---- sepal_ui__deb5b9ab__base : steps -8 -5 6 8 ----
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__deb5b9ab__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step -8 --suffix githist-m8 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__deb5b9ab__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step -5 --suffix githist-m5 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__deb5b9ab__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step 6 --suffix githist-p6 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push
uv run sourceworldbench-benchmarks augment git-history-main --base-instance-id sepal_ui__deb5b9ab__base \
  --report data/examples/git_augment_rebench/git_history/sepal_ui__main.json --step 8 --suffix githist-p8 \
  --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.partial.jsonl --no-push


# =============================================================
# D. Run every partial datapoint and fill its test outcomes.
#    Only the 24 unfilled augmented rows are collected; the 6
#    base rows are already filled and skipped automatically.
# =============================================================
uv run sourceworldbench-benchmarks collect --in data/examples/git_augment_rebench/git_augmented.partial.jsonl --out data/examples/git_augment_rebench/git_augmented.filled.jsonl \
  --no-push --timeout 900
