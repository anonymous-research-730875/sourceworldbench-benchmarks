#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
python -m pytest tests/ \
  --ignore=tests/test_end_to_end.py \
  --deselect tests/integration/test_search_stac_static.py::TestSearchStacStatic::test_search_stac_static_load_item_updated_provider \
  --deselect tests/units/test_stac_utils.py::TestStacUtils::test_get_product_types \
  --no-header -rA --tb=line --color=no \
  -p no:cacheprovider \
  -W ignore::DeprecationWarning \
  --junitxml=/results/junit.xml \
  --continue-on-collection-errors
