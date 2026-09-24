#!/usr/bin/env bash
set -uo pipefail

mkdir -p /results

# Semantics suite (its own hatch dev-env; run from the sub-project dir).
cd /app/metricflow-semantics
MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com \
  /app/metricflow-semantics/.venv/bin/pytest tests_metricflow_semantics/ \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_set_equals \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_set_in \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_tuple_equals \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_create_new \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_create_existing \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_hash \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_in_set \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_in_dict \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_create \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_equals \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_field_access \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_lt \
  --deselect metricflow-semantics/tests_metricflow_semantics/test_benchmark.py::test_assert_performance_factor \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_init_time \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_query_time \
  --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/dsi/test_manifest_object_lookup.py::test_model_lookup_performance \
  -n auto --junitxml=/results/junit_semantics.xml -q --continue-on-collection-errors

# Main MetricFlow suite (root hatch dev-env; DuckDB engine).
cd /app
MF_TEST_ADAPTER_TYPE=duckdb MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com PYTHONPATH=metricflow-semantics:dbt-metricflow \
  /app/.venv/bin/pytest tests_metricflow/ \
  --deselect tests_metricflow/performance/test_mf_engine.py::test_init_time \
  -n auto --junitxml=/results/junit_metricflow.xml -q --continue-on-collection-errors
