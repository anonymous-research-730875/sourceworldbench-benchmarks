#!/usr/bin/env bash
set -uo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest tests_metricflow_semantics --deselect tests_metricflow_semantics/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_query_time --deselect tests_metricflow_semantics/semantic_graph/lookups/test_manifest_object_lookup.py::test_model_lookup_performance --deselect tests_metricflow_semantics/test_benchmark.py::test_assert_performance_factor --deselect tests_metricflow_semantics/toolkit/test_fast_frozen_dataclass.py::test_hash --junitxml=/results/junit_semantics.xml -n auto -q --continue-on-collection-errors
/app/.venv/bin/pytest tests_metricflow_semantic_interfaces --junitxml=/results/junit_si.xml -n auto -q --continue-on-collection-errors
/app/.venv/bin/pytest tests_metricflow --junitxml=/results/junit_metricflow.xml -n auto -q --continue-on-collection-errors
