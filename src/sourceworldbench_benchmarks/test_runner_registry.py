# ruff: noqa: E501

from sourceworldbench_benchmarks.schema import BaseKey

TestCommand = str
TestScope = list[str]

DEFAULT_RUN_TESTS_COMMAND_REGISTRY: dict[BaseKey, TestCommand] = {
    (
        "Textualize/rich",
        "46cebbb032f920eb096efbaf23cdc6fe9dd541f7",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "Textualize/rich",
        "efe8f619138327dd3e8fe490289d1b0e6da4a0e8",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "Textualize/rich",
        "a34914be5d1cc9dc298ca92e5c9af757157a0bb7",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "pallets-eco/flask-wtf",
        "8849b06bebd909a0172b1dfa746b092466d48683",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "pallets-eco/flask-wtf",
        "de1faa6ba9ab17a0c353d3a211655d42b8d11bb3",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "pallets-eco/flask-wtf",
        "ea1f797112f857c783dcd2c6e3954357df8e1bb7",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "pallets-eco/flask-wtf",
        "2e142951788d8209815fd4797c93f6f3c275ed71",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "pallets-eco/flask-wtf",
        "e243777fed50915086326297fbfe789a935afffa",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "hvac/hvac",
        "09902dea41c2c6313cd7844be4d984c1514bd822",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit_tests -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "hvac/hvac",
        "7ae23dfe283f43ca6b355080494e6110cc46da8d",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit_tests -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "hvac/hvac",
        "5a9ed4a683f20b30c0849456d78110baf11a616d",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit_tests -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "hvac/hvac",
        "b1f62ace2101f973b5ef2e42b143bccbdd020513",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit_tests -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "hvac/hvac",
        "f0b44cc757179d088780bbd4c5fd87cc6e264766",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit_tests -q --junit-xml=/results/results.xml --continue-on-collection-errors",
    (
        "dbt-labs/metricflow",
        "e062a42e581ac73004bb0312ac33c7ec31dd0d6d",
    ): "mkdir -p /results && cd /app && METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com /app/.venv/bin/pytest metricflow/test/ -n 4 --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "dbt-labs/metricflow",
        "46bd7bf505c4c732e5db14bedd301ea3e930ed6c",
    ): "mkdir -p /results && cd /app/metricflow-semantics && MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com /app/metricflow-semantics/.venv/bin/pytest tests_metricflow_semantics/ --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_set_equals --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_set_in --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_tuple_equals --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_create_new --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_singleton.py::test_create_existing --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_hash --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_in_set --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_in_dict --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_create --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_equals --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_field_access --deselect metricflow-semantics/tests_metricflow_semantics/experimental/collection_helpers/test_fast_frozen_dataclass.py::test_lt --deselect metricflow-semantics/tests_metricflow_semantics/test_benchmark.py::test_assert_performance_factor --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_init_time --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_query_time --deselect metricflow-semantics/tests_metricflow_semantics/experimental/semantic_graph/dsi/test_manifest_object_lookup.py::test_model_lookup_performance -n auto --junitxml=/results/junit_semantics.xml -q --continue-on-collection-errors ; cd /app && MF_TEST_ADAPTER_TYPE=duckdb MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com PYTHONPATH=metricflow-semantics:dbt-metricflow /app/.venv/bin/pytest tests_metricflow/ --deselect tests_metricflow/performance/test_mf_engine.py::test_init_time -n auto --junitxml=/results/junit_metricflow.xml -q --continue-on-collection-errors",
    (
        "dbt-labs/metricflow",
        "2b49b34f34a247173569e2d6e59620ec74dd32a1",
    ): "mkdir -p /results && cd /app/metricflow-semantics && MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com /app/metricflow-semantics/.venv/bin/pytest tests_metricflow_semantics/ -n auto --junitxml=/results/junit_semantics.xml -q --continue-on-collection-errors ; cd /app && MF_TEST_ADAPTER_TYPE=duckdb MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com PYTHONPATH=metricflow-semantics:dbt-metricflow /app/.venv/bin/pytest tests_metricflow/ -n auto --junitxml=/results/junit_metricflow.xml -q --continue-on-collection-errors",
    (
        "dbt-labs/metricflow",
        "9b37b0aae4d1052b826b17f4f95734ef5fa14a78",
    ): "mkdir -p /results && cd /app/metricflow-semantics && MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com /app/metricflow-semantics/.venv/bin/pytest tests_metricflow_semantics/ -n auto --junitxml=/results/junit_semantics.xml -q --continue-on-collection-errors ; cd /app && MF_TEST_ADAPTER_TYPE=duckdb MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com PYTHONPATH=metricflow-semantics:dbt-metricflow /app/.venv/bin/pytest tests_metricflow/ -n auto --junitxml=/results/junit_metricflow.xml -q --continue-on-collection-errors",
    (
        "dbt-labs/metricflow",
        "a6ac795fd06dbc630c2cae3a3093417ba0fde530",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests_metricflow_semantics --deselect tests_metricflow_semantics/semantic_graph/resolver/test_sg_resolver_performance.py::test_resolver_query_time --deselect tests_metricflow_semantics/semantic_graph/lookups/test_manifest_object_lookup.py::test_model_lookup_performance --deselect tests_metricflow_semantics/test_benchmark.py::test_assert_performance_factor --deselect tests_metricflow_semantics/toolkit/test_fast_frozen_dataclass.py::test_hash --junitxml=/results/junit_semantics.xml -n auto -q --continue-on-collection-errors ; /app/.venv/bin/pytest tests_metricflow_semantic_interfaces --junitxml=/results/junit_si.xml -n auto -q --continue-on-collection-errors ; /app/.venv/bin/pytest tests_metricflow --junitxml=/results/junit_metricflow.xml -n auto -q --continue-on-collection-errors",
    (
        "Nixtla/neuralforecast",
        "a788a8b4b2434e55524bee2a3c0e72b3b0c1dba2",
    ): "mkdir -p /results && cd /app && for f in $(/app/.venv/bin/pytest tests --co -q --no-cov 2>/dev/null | grep :: | sed 's/::.*//' | sort -u); do /app/.venv/bin/pytest $f --no-cov --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' --junitxml=/results/junit_$(echo $f | tr '/.' '__').xml -q --continue-on-collection-errors; done",
    (
        "Nixtla/neuralforecast",
        "130b7f1d4b1cc766dd0f0db65a1dbec392f1a057",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --no-cov --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "Nixtla/neuralforecast",
        "9bc90c1eac5b717a46d6152106e38932fb9c3dc6",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --no-cov --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "Nixtla/neuralforecast",
        "3ff61fe20592b135cc3272172b5881293395b3b1",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --no-cov --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "Nixtla/neuralforecast",
        "d281a5ee659be3607d9b9fefdb7e45913b5e8819",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --no-cov --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "theskumar/python-dotenv",
        "3f0e3e0b461cf7127e2753efbdf896bdff31db2e",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "theskumar/python-dotenv",
        "b5b8ae20d0bc0852b22e428d138ce7346c7a11bc",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "theskumar/python-dotenv",
        "fa4e6a90b45428212452afc6ee0d5c8103b9301d",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "theskumar/python-dotenv",
        "d0a219d180ec62bfb1bead5849aecca91eead3d9",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "theskumar/python-dotenv",
        "3af77d3029eb717aeec0a3c25f751b6a614a6d3c",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "RocketPy-Team/RocketPy",
        "3ce20f06f2709d1058fb4317304b8c4246a4b248",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests --deselect tests/test_environment.py::test_wyoming_sounding_atmosphere --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "RocketPy-Team/RocketPy",
        "1e748d8819b37ddecd783399037dee4ee7fafa13",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit tests/integration rocketpy --doctest-modules tests/acceptance --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "RocketPy-Team/RocketPy",
        "7452b6145a8ab1df02310cdd370eda1138294f6b",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit tests/integration tests/test_tank.py rocketpy --doctest-modules tests/acceptance --deselect tests/integration/test_environment.py::test_wyoming_sounding_atmosphere --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "RocketPy-Team/RocketPy",
        "1544355066ab26e996c52538e6d4a455a20bdfc7",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit tests/integration rocketpy --doctest-modules tests/acceptance --deselect 'tests/integration/environment/test_environment.py::test_windy_atmosphere' --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "RocketPy-Team/RocketPy",
        "af9057b134dbd489b1297a6a2cd36c174682568d",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/unit tests/integration rocketpy --doctest-modules tests/acceptance --deselect tests/integration/test_environment.py::test_wyoming_sounding_atmosphere --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "jd/tenacity",
        "423848ce328326542a0287c609c21e3d76ef7873",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tenacity/tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "jd/tenacity",
        "55aa17c8cc018aecda1828e8fbc64840165549fe",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tenacity/tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "jd/tenacity",
        "a7b83aeb487bf8de6b8f601d5b2e35f7141e8a4e",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tenacity/tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "jd/tenacity",
        "e8d5f3b91b4da34e799c9aeff29e3ee3631047c4",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "jd/tenacity",
        "8a171aa5422efac9bd144d6dcd33d703359647d6",
    ): "mkdir -p /results && cd /app && /app/.venv/bin/pytest tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors",
    (
        "12rambau/sepal_ui",
        "179bd8d089275c54e94a7614be7ed03d298ef532",
    ): "mkdir -p /results && cd /app && /usr/local/bin/pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning --junitxml=/results/junit.xml --continue-on-collection-errors tests/",
    (
        "Azure/azure-cli",
        "85c0e4c8fb21e26a4984cad3b21a5e22a6a8b92b",
    ): "mkdir -p /results && cd /app && source /opt/conda/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "NCAR/geocat-comp",
        "9ccf3e0cb4575c11f41288cdc95a0a8f6f3875ac",
    ): "mkdir -p /results && cd /app && python -m pytest test --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "CS-SI/eodag",
        "0a782d7f476a993170f7d60c006be09b4cfa2b47",
    ): "mkdir -p /results && cd /app && python -m pytest tests/ --ignore=tests/test_end_to_end.py --deselect tests/integration/test_search_stac_static.py::TestSearchStacStatic::test_search_stac_static_load_item_updated_provider --deselect tests/units/test_stac_utils.py::TestStacUtils::test_get_product_types --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning --junitxml=/results/junit.xml --continue-on-collection-errors",
    # swefficiency
    (
        "scipy/scipy",
        "98e28806dc0985fdb8749fdbbe3d78700f29fbe4",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "4c8d66ecfe2b13607afd254443979b1ff842b6c1",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "b73c38e20518d874bceaaab6c3efdb034f36ad54",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "b8c8aca6d22f0560b2bd5551f77f515f37377087",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "2617bfc43304dbdae3aab75160ef9a776afadd7a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "b8cce91ee7bcc86877d4679cd8a9454b5995c2c6",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "32999a1a45f2c4171b8ac5e97f5aa34b1956686c",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "d36138b116a9fa214506160711d8bebb335a3c33",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "71fc89cd515c3c19230fbab64e979118858b808a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "a80ffdb19988d175f3e54a9c6e472e4ff6b8cbc0",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "7888cf47e509bc61871c599ee1b636c0f98c9076",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "103d3b2bb8912c87f3ec11c1048e7e0a19225fef",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "012575a82f4a80c1b87b6f3390ef8de757aeabec",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "62227895d26bfccfc78260ce0dddaebeab0672fe",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "9eb15535662ac80bef1522b483fc2a6a42e0e192",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "pandas-dev/pandas",
        "1f622e2b5303650fa5e497e4552d0554e51049cb",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m \"not slow and not network and not db and not single_cpu\" --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "ff57f89ef5307a91d1d4fe6e82eb87d29668b27a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "dd8ccdde27858d076b8dbb676364e520ca06f987",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "dd5b594334c43ff504e035d94f3e5ed017ff1d14",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "31c6b608ec26b27fe61146699f2ffe8775474b43",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "1efaab7bb3c117a14d3f6accf77239a44e59e0df",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "cdf311e0714e611d48b0a31eb1f0e2cbffab7f23",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "e262c71045449433040980555074d0941dd6230f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "0da28efef20e0c2acb59d8e22048c1d92e496610",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "ea875472867f296eee3ed75989ed402d55587940",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "7ddf5ee8f3939db79cabebd627fad724c4d8e872",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "96cc7fbefd59e79d096802efe9f75b2f1d042487",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "33265f16ebb00d3c6c5812911df0d9b4e1bbb8e0",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "769ad41652038d7d2aba9657a28f0ce8aaac982d",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "f5126c765a6a8db8abc7504275d9c2e90ffbd526",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "ff59e9cffcd7145f0c8c68be3d4e8426348c055e",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "6aca9bf6909c364984403e194891e592666fe198",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "f788114c8a01a966cebdc8710674f88f9f1cdb7c",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "3ff2463d061e933eaf5c1a1c110edf412c784d4b",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "d09ba92c28a0e22c27e7dfcdd2ed45d3bb7fa80d",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "157e90a7b1cc995ce7a4eb100167134904ca84fe",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "9818492021c57ba2a879fd810c6e6cd1d1f3d8fe",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "1b97e5cc28a027c7ff9a5361ae60d4b7034cbb1d",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "1210dab3c0828e6ec5aee15fe11caec267a20b0a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "ee6d681a230cb9aac2eec4a5d056eaea9a44288f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "09d963266765e3fb0bb907792e5a65d6dd42f925",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "05caa0d16a1905a292888a56829df2420bad92f9",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "b5f7c4da726ef252d12b0c933bfbd5ba3d07818d",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "d74d02fba817a647af5ff56ec874df0a1a39e9f4",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "999a3a44edcf43745e544c21d0d8c33a16db26f7",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "1cb6af9ab04ae4208e3f02c0841fa7cec7b276c5",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "998f666b8349cf758eceb1fc140d9b8e7258ff88",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "8d97a4f9d62a76629ac9b86ebd1c22c340ea7a0e",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "987be7297fed38e9fba0047419fe54cbe5f0d709",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "33db190c86941ea8b120814813d9fd4a5d423768",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "astropy/astropy",
        "387c3d28653706ad59e689bd3755b9e92f91305f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "bdc71ba5a4ad7bfb0265a52a1b816936604845fe",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "a8f2acc2e3328707cda2a25cee6cfadec44ec815",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "07099e5c0385983ad1cab805e42e66d03fc3f6d3",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "fe465e055293851a1920bd9a318c92d1953a8b65",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "6d878e68248a12f15bb958951111efe8b030d9e8",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "09f7b637a54280f395a06963c5af115ce42027f3",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "d3798151747297a88604e2f853f830f579f7e602",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "5f61f7f79b861be5ffdbf59950889166628a8fa6",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "54deb7fa4cdbbf3e124be6834224272be27ace65",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "72c7d906212f14df5d515be33c9cc5f0d86b3fec",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "9968a49bb4e9ae03d29110bbc5a41a4179636ab8",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "8eeb0e0194ef0561b4202f42de06c0b7fc0784b9",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "e5acf325ecc7af9147fe7644a1548380b54d5364",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "ee0d205ce8bec289cefd5989b7af0d5cda67fe8b",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "4a7a2438219c4ee493434042e50f4cdb67b6ec9f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "0b33708f4627c3f9c7613c9554d1033db147d4b0",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "3d0e7bd93d6c6c67208765fd0a81f13088bb53a7",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "dask/dask",
        "a1187b13321d69565b9c21359d739c239bd04c65",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W \"ignore::DeprecationWarning\"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "3f9389ef260a1c22db801c0744e9a2b941b0c9dc",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "8960dbb2d467e4d70980e98797c6e28253d27caf",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "ba7a162c01efbccce7432b5936bd5cc5b72b810c",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "2555e38ec977cb2432bb10876254bbe8721e07f5",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "91ad8671e8b8bd55a5de68bc17b18dcb67a5d6ac",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "57c8baaf85f0cfd44a27ef834dc971128b7f8ee4",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "096d0ac1537f396528cdea0709e7d9d4125478df",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "019a75231fd8d475c8b6f42a7d0cb1d99e9dcfa5",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "cf83ec4f46ae03668a1d89273b28147497ab65f2",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "183018c13beefd2604f443678cef42fab4c0f13a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "bfa31a482d6baa9a6da417bc1c20d4cd93abcece",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "b525983fb5ad295ee8a789df46989574b452369a",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "68d6b79654626f1471caf9dd60d9f728823f0d90",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "709fba81d42d39a7cdde22e7d3c790ec4090a3f3",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "00cdf28e7adf2216d41fc28b6eebcee3b8217d5f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "5823ec9ef15dd169645c026684cb746de44223e9",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "060992a18e1f9d6a370f73c02583f314631bc96f",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "aee2ea3c3537fe5551c8737a1a580152c89ca0e6",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "7d2acfda30077403682d8beef5a3c340ac98a43b",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "matplotlib/matplotlib",
        "8d3c4db01c59592fa612b093beb9e3e5ad959191",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "ada168000ee3a6727af4eb02cf2443edbf58e962",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "9cc9f01da14d0eb7904dfe4351d393ff57795e15",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "38236fb225418ad5979511e3caf7e992df72b1a7",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "6b8441665fb877ab65ec067de7be776ff4f5ac54",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "6eb63c386f83143aa8d256f44ce850e4e2ee0785",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "4b4eaa666b18016162c144b7757ba40d8237fdb8",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "fe7b1dcfc16fd9a8a1e6ea24ea103abf870282ed",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "61723384df3678b12a19439d1218f50554e1498d",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "a1ee7968df16a4f57c8e22164d19aa2d14a6cdad",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "f25905b5d25a2fca1e23adad11d4597a1e658276",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    (
        "numpy/numpy",
        "b5cdbf0434019e802b5e1a44bee06e67e0beef75",
    ): "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors",
    # insert here 1
    
}


DEFAULT_TEST_SCOPE_REGISTRY: dict[BaseKey, TestScope] = {
    ("Textualize/rich", "46cebbb032f920eb096efbaf23cdc6fe9dd541f7"): ["tests/"],
    ("Textualize/rich", "efe8f619138327dd3e8fe490289d1b0e6da4a0e8"): ["tests/"],
    ("Textualize/rich", "a34914be5d1cc9dc298ca92e5c9af757157a0bb7"): ["tests/"],
    ("pallets-eco/flask-wtf", "8849b06bebd909a0172b1dfa746b092466d48683"): ["tests/"],
    ("pallets-eco/flask-wtf", "de1faa6ba9ab17a0c353d3a211655d42b8d11bb3"): ["tests/"],
    ("pallets-eco/flask-wtf", "ea1f797112f857c783dcd2c6e3954357df8e1bb7"): ["tests/"],
    ("pallets-eco/flask-wtf", "2e142951788d8209815fd4797c93f6f3c275ed71"): ["tests/"],
    ("pallets-eco/flask-wtf", "e243777fed50915086326297fbfe789a935afffa"): ["tests/"],
    ("hvac/hvac", "09902dea41c2c6313cd7844be4d984c1514bd822"): ["tests/"],
    ("hvac/hvac", "7ae23dfe283f43ca6b355080494e6110cc46da8d"): ["tests/"],
    ("hvac/hvac", "5a9ed4a683f20b30c0849456d78110baf11a616d"): ["tests/"],
    ("hvac/hvac", "b1f62ace2101f973b5ef2e42b143bccbdd020513"): ["tests/"],
    ("hvac/hvac", "f0b44cc757179d088780bbd4c5fd87cc6e264766"): ["tests/"],
    ("dbt-labs/metricflow", "e062a42e581ac73004bb0312ac33c7ec31dd0d6d"): ["metricflow/test/"],
    ("dbt-labs/metricflow", "46bd7bf505c4c732e5db14bedd301ea3e930ed6c"): [
        "tests_metricflow/",
        "metricflow-semantics/tests_metricflow_semantics/",
    ],
    ("dbt-labs/metricflow", "2b49b34f34a247173569e2d6e59620ec74dd32a1"): [
        "tests_metricflow/",
        "metricflow-semantics/tests_metricflow_semantics/",
    ],
    ("dbt-labs/metricflow", "9b37b0aae4d1052b826b17f4f95734ef5fa14a78"): [
        "tests_metricflow/",
        "metricflow-semantics/tests_metricflow_semantics/",
    ],
    ("dbt-labs/metricflow", "a6ac795fd06dbc630c2cae3a3093417ba0fde530"): [
        "tests_metricflow_semantics/",
        "tests_metricflow_semantic_interfaces/",
        "tests_metricflow/",
    ],
    ("Nixtla/neuralforecast", "a788a8b4b2434e55524bee2a3c0e72b3b0c1dba2"): ["tests/"],
    ("Nixtla/neuralforecast", "130b7f1d4b1cc766dd0f0db65a1dbec392f1a057"): ["tests/"],
    ("Nixtla/neuralforecast", "9bc90c1eac5b717a46d6152106e38932fb9c3dc6"): ["tests/"],
    ("Nixtla/neuralforecast", "3ff61fe20592b135cc3272172b5881293395b3b1"): ["tests/"],
    ("Nixtla/neuralforecast", "d281a5ee659be3607d9b9fefdb7e45913b5e8819"): ["tests/"],
    ("theskumar/python-dotenv", "3f0e3e0b461cf7127e2753efbdf896bdff31db2e"): ["tests/"],
    ("theskumar/python-dotenv", "b5b8ae20d0bc0852b22e428d138ce7346c7a11bc"): ["tests/"],
    ("theskumar/python-dotenv", "fa4e6a90b45428212452afc6ee0d5c8103b9301d"): ["tests/"],
    ("theskumar/python-dotenv", "d0a219d180ec62bfb1bead5849aecca91eead3d9"): ["tests/"],
    ("theskumar/python-dotenv", "3af77d3029eb717aeec0a3c25f751b6a614a6d3c"): ["tests/"],
    ("RocketPy-Team/RocketPy", "3ce20f06f2709d1058fb4317304b8c4246a4b248"): ["tests/"],
    ("RocketPy-Team/RocketPy", "1e748d8819b37ddecd783399037dee4ee7fafa13"): ["tests/"],
    ("RocketPy-Team/RocketPy", "7452b6145a8ab1df02310cdd370eda1138294f6b"): ["tests/"],
    ("RocketPy-Team/RocketPy", "1544355066ab26e996c52538e6d4a455a20bdfc7"): ["tests/"],
    ("RocketPy-Team/RocketPy", "af9057b134dbd489b1297a6a2cd36c174682568d"): ["tests/"],
    ("jd/tenacity", "423848ce328326542a0287c609c21e3d76ef7873"): ["tenacity/tests/"],
    ("jd/tenacity", "55aa17c8cc018aecda1828e8fbc64840165549fe"): ["tenacity/tests/"],
    ("jd/tenacity", "a7b83aeb487bf8de6b8f601d5b2e35f7141e8a4e"): ["tenacity/tests/"],
    ("jd/tenacity", "e8d5f3b91b4da34e799c9aeff29e3ee3631047c4"): ["tests/"],
    ("jd/tenacity", "8a171aa5422efac9bd144d6dcd33d703359647d6"): ["tests/"],
    ("12rambau/sepal_ui", "179bd8d089275c54e94a7614be7ed03d298ef532"): ["tests/"],
    ("Azure/azure-cli", "85c0e4c8fb21e26a4984cad3b21a5e22a6a8b92b"): [],
    ("NCAR/geocat-comp", "9ccf3e0cb4575c11f41288cdc95a0a8f6f3875ac"): ["test/"],
    ("CS-SI/eodag", "0a782d7f476a993170f7d60c006be09b4cfa2b47"): ["tests/"],
    # swefficiency
    ("scipy/scipy", "98e28806dc0985fdb8749fdbbe3d78700f29fbe4"): ["scipy/stats/tests/test_kdeoth.py"],
    ("pandas-dev/pandas", "4c8d66ecfe2b13607afd254443979b1ff842b6c1"): ['tests/'],
    ("pandas-dev/pandas", "b73c38e20518d874bceaaab6c3efdb034f36ad54"): ['tests/'],
    ("pandas-dev/pandas", "b8c8aca6d22f0560b2bd5551f77f515f37377087"): ['tests/'],
    ("pandas-dev/pandas", "2617bfc43304dbdae3aab75160ef9a776afadd7a"): ['tests/'],
    ("pandas-dev/pandas", "b8cce91ee7bcc86877d4679cd8a9454b5995c2c6"): ['tests/'],
    ("pandas-dev/pandas", "32999a1a45f2c4171b8ac5e97f5aa34b1956686c"): ['tests/'],
    ("pandas-dev/pandas", "d36138b116a9fa214506160711d8bebb335a3c33"): ['tests/'],
    ("pandas-dev/pandas", "71fc89cd515c3c19230fbab64e979118858b808a"): ['tests/'],
    ("pandas-dev/pandas", "a80ffdb19988d175f3e54a9c6e472e4ff6b8cbc0"): ['tests/'],
    ("pandas-dev/pandas", "7888cf47e509bc61871c599ee1b636c0f98c9076"): ['tests/'],
    ("pandas-dev/pandas", "103d3b2bb8912c87f3ec11c1048e7e0a19225fef"): ['tests/'],
    ("pandas-dev/pandas", "012575a82f4a80c1b87b6f3390ef8de757aeabec"): ['tests/'],
    ("pandas-dev/pandas", "62227895d26bfccfc78260ce0dddaebeab0672fe"): ['tests/'],
    ("pandas-dev/pandas", "9eb15535662ac80bef1522b483fc2a6a42e0e192"): ['tests/'],
    ("pandas-dev/pandas", "1f622e2b5303650fa5e497e4552d0554e51049cb"): ['tests/'],
    ("astropy/astropy", "ff57f89ef5307a91d1d4fe6e82eb87d29668b27a"): ['tests/'],
    ("astropy/astropy", "dd8ccdde27858d076b8dbb676364e520ca06f987"): ['tests/'],
    ("astropy/astropy", "dd5b594334c43ff504e035d94f3e5ed017ff1d14"): ['tests/'],
    ("astropy/astropy", "31c6b608ec26b27fe61146699f2ffe8775474b43"): ['tests/'],
    ("astropy/astropy", "1efaab7bb3c117a14d3f6accf77239a44e59e0df"): ['tests/'],
    ("astropy/astropy", "cdf311e0714e611d48b0a31eb1f0e2cbffab7f23"): ['tests/'],
    ("astropy/astropy", "e262c71045449433040980555074d0941dd6230f"): ['tests/'],
    ("astropy/astropy", "0da28efef20e0c2acb59d8e22048c1d92e496610"): ['tests/'],
    ("astropy/astropy", "ea875472867f296eee3ed75989ed402d55587940"): ['tests/'],
    ("astropy/astropy", "7ddf5ee8f3939db79cabebd627fad724c4d8e872"): ['tests/'],
    ("astropy/astropy", "96cc7fbefd59e79d096802efe9f75b2f1d042487"): ['tests/'],
    ("astropy/astropy", "33265f16ebb00d3c6c5812911df0d9b4e1bbb8e0"): ['tests/'],
    ("astropy/astropy", "769ad41652038d7d2aba9657a28f0ce8aaac982d"): ['tests/'],
    ("astropy/astropy", "f5126c765a6a8db8abc7504275d9c2e90ffbd526"): ['tests/'],
    ("astropy/astropy", "ff59e9cffcd7145f0c8c68be3d4e8426348c055e"): ['tests/'],
    ("astropy/astropy", "6aca9bf6909c364984403e194891e592666fe198"): ['tests/'],
    ("astropy/astropy", "f788114c8a01a966cebdc8710674f88f9f1cdb7c"): ['tests/'],
    ("astropy/astropy", "3ff2463d061e933eaf5c1a1c110edf412c784d4b"): ['tests/'],
    ("astropy/astropy", "d09ba92c28a0e22c27e7dfcdd2ed45d3bb7fa80d"): ['tests/'],
    ("astropy/astropy", "157e90a7b1cc995ce7a4eb100167134904ca84fe"): ['tests/'],
    ("astropy/astropy", "9818492021c57ba2a879fd810c6e6cd1d1f3d8fe"): ['tests/'],
    ("astropy/astropy", "1b97e5cc28a027c7ff9a5361ae60d4b7034cbb1d"): ['tests/'],
    ("astropy/astropy", "1210dab3c0828e6ec5aee15fe11caec267a20b0a"): ['tests/'],
    ("astropy/astropy", "ee6d681a230cb9aac2eec4a5d056eaea9a44288f"): ['tests/'],
    ("astropy/astropy", "09d963266765e3fb0bb907792e5a65d6dd42f925"): ['tests/'],
    ("astropy/astropy", "05caa0d16a1905a292888a56829df2420bad92f9"): ['tests/'],
    ("astropy/astropy", "b5f7c4da726ef252d12b0c933bfbd5ba3d07818d"): ['tests/'],
    ("astropy/astropy", "d74d02fba817a647af5ff56ec874df0a1a39e9f4"): ['tests/'],
    ("astropy/astropy", "999a3a44edcf43745e544c21d0d8c33a16db26f7"): ['tests/'],
    ("astropy/astropy", "1cb6af9ab04ae4208e3f02c0841fa7cec7b276c5"): ['tests/'],
    ("astropy/astropy", "998f666b8349cf758eceb1fc140d9b8e7258ff88"): ['tests/'],
    ("astropy/astropy", "8d97a4f9d62a76629ac9b86ebd1c22c340ea7a0e"): ['tests/'],
    ("astropy/astropy", "987be7297fed38e9fba0047419fe54cbe5f0d709"): ['tests/'],
    ("astropy/astropy", "33db190c86941ea8b120814813d9fd4a5d423768"): ['tests/'],
    ("astropy/astropy", "387c3d28653706ad59e689bd3755b9e92f91305f"): ['tests/'],
    ("dask/dask", "bdc71ba5a4ad7bfb0265a52a1b816936604845fe"): ['tests/'],
    ("dask/dask", "a8f2acc2e3328707cda2a25cee6cfadec44ec815"): ['tests/'],
    ("dask/dask", "07099e5c0385983ad1cab805e42e66d03fc3f6d3"): ['tests/'],
    ("dask/dask", "fe465e055293851a1920bd9a318c92d1953a8b65"): ['tests/'],
    ("dask/dask", "6d878e68248a12f15bb958951111efe8b030d9e8"): ['tests/'],
    ("dask/dask", "09f7b637a54280f395a06963c5af115ce42027f3"): ['tests/'],
    ("dask/dask", "d3798151747297a88604e2f853f830f579f7e602"): ['tests/'],
    ("dask/dask", "5f61f7f79b861be5ffdbf59950889166628a8fa6"): ['tests/'],
    ("dask/dask", "54deb7fa4cdbbf3e124be6834224272be27ace65"): ['tests/'],
    ("dask/dask", "72c7d906212f14df5d515be33c9cc5f0d86b3fec"): ['tests/'],
    ("dask/dask", "9968a49bb4e9ae03d29110bbc5a41a4179636ab8"): ['tests/'],
    ("dask/dask", "8eeb0e0194ef0561b4202f42de06c0b7fc0784b9"): ['tests/'],
    ("dask/dask", "e5acf325ecc7af9147fe7644a1548380b54d5364"): ['tests/'],
    ("dask/dask", "ee0d205ce8bec289cefd5989b7af0d5cda67fe8b"): ['tests/'],
    ("dask/dask", "4a7a2438219c4ee493434042e50f4cdb67b6ec9f"): ['tests/'],
    ("dask/dask", "0b33708f4627c3f9c7613c9554d1033db147d4b0"): ['tests/'],
    ("dask/dask", "3d0e7bd93d6c6c67208765fd0a81f13088bb53a7"): ['tests/'],
    ("dask/dask", "a1187b13321d69565b9c21359d739c239bd04c65"): ['tests/'],
    ("matplotlib/matplotlib", "3f9389ef260a1c22db801c0744e9a2b941b0c9dc"): ['tests/'],
    ("matplotlib/matplotlib", "8960dbb2d467e4d70980e98797c6e28253d27caf"): ['tests/'],
    ("matplotlib/matplotlib", "ba7a162c01efbccce7432b5936bd5cc5b72b810c"): ['tests/'],
    ("matplotlib/matplotlib", "2555e38ec977cb2432bb10876254bbe8721e07f5"): ['tests/'],
    ("matplotlib/matplotlib", "91ad8671e8b8bd55a5de68bc17b18dcb67a5d6ac"): ['tests/'],
    ("matplotlib/matplotlib", "57c8baaf85f0cfd44a27ef834dc971128b7f8ee4"): ['tests/'],
    ("matplotlib/matplotlib", "096d0ac1537f396528cdea0709e7d9d4125478df"): ['tests/'],
    ("matplotlib/matplotlib", "019a75231fd8d475c8b6f42a7d0cb1d99e9dcfa5"): ['tests/'],
    ("matplotlib/matplotlib", "cf83ec4f46ae03668a1d89273b28147497ab65f2"): ['tests/'],
    ("matplotlib/matplotlib", "183018c13beefd2604f443678cef42fab4c0f13a"): ['tests/'],
    ("matplotlib/matplotlib", "bfa31a482d6baa9a6da417bc1c20d4cd93abcece"): ['tests/'],
    ("matplotlib/matplotlib", "b525983fb5ad295ee8a789df46989574b452369a"): ['tests/'],
    ("matplotlib/matplotlib", "68d6b79654626f1471caf9dd60d9f728823f0d90"): ['tests/'],
    ("matplotlib/matplotlib", "709fba81d42d39a7cdde22e7d3c790ec4090a3f3"): ['tests/'],
    ("matplotlib/matplotlib", "00cdf28e7adf2216d41fc28b6eebcee3b8217d5f"): ['tests/'],
    ("matplotlib/matplotlib", "5823ec9ef15dd169645c026684cb746de44223e9"): ['tests/'],
    ("matplotlib/matplotlib", "060992a18e1f9d6a370f73c02583f314631bc96f"): ['tests/'],
    ("matplotlib/matplotlib", "aee2ea3c3537fe5551c8737a1a580152c89ca0e6"): ['tests/'],
    ("matplotlib/matplotlib", "7d2acfda30077403682d8beef5a3c340ac98a43b"): ['tests/'],
    ("matplotlib/matplotlib", "8d3c4db01c59592fa612b093beb9e3e5ad959191"): ['tests/'],
    ("numpy/numpy", "ada168000ee3a6727af4eb02cf2443edbf58e962"): ['tests/'],
    ("numpy/numpy", "9cc9f01da14d0eb7904dfe4351d393ff57795e15"): ['tests/'],
    ("numpy/numpy", "38236fb225418ad5979511e3caf7e992df72b1a7"): ['tests/'],
    ("numpy/numpy", "6b8441665fb877ab65ec067de7be776ff4f5ac54"): ['tests/'],
    ("numpy/numpy", "6eb63c386f83143aa8d256f44ce850e4e2ee0785"): ['tests/'],
    ("numpy/numpy", "4b4eaa666b18016162c144b7757ba40d8237fdb8"): ['tests/'],
    ("numpy/numpy", "fe7b1dcfc16fd9a8a1e6ea24ea103abf870282ed"): ['tests/'],
    ("numpy/numpy", "61723384df3678b12a19439d1218f50554e1498d"): ['tests/'],
    ("numpy/numpy", "a1ee7968df16a4f57c8e22164d19aa2d14a6cdad"): ['tests/'],
    ("numpy/numpy", "f25905b5d25a2fca1e23adad11d4597a1e658276"): ['tests/'],
    ("numpy/numpy", "b5cdbf0434019e802b5e1a44bee06e67e0beef75"): ['tests/'],
    # insert here 2
}
