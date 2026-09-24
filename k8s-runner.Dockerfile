# The runner binary is injected into every environment image and must run in
# all of them. The oldest environments are bullseye-based (glibc 2.31), so the
# binary is built on bullseye with the system interpreter — PyInstaller bundles
# that libpython, which by construction cannot require newer glibc symbols.
FROM python:3.13-slim-bullseye AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# A uv-managed CPython would silently re-bundle a newer-glibc libpython,
# which is exactly the incompatibility this base image exists to prevent.
ENV UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev
# --collect-all bundles sourceworldbench_benchmarks' modules as bytecode, but the tracer
# bootstrap/tracers .py files are read as *source* at runtime (_stage_bundle
# copies them into a PYTHONPATH bundle dir), so they must also ship as data at
# their package-relative paths for __file__ resolution to find them in-pod.
RUN uv run --no-dev --with pyinstaller pyinstaller \
    --onefile \
    --name sourceworldbench-benchmarks \
    --collect-all sourceworldbench_benchmarks \
    --add-data src/sourceworldbench_benchmarks/execution_tracer/tracers:sourceworldbench_benchmarks/execution_tracer/tracers \
    --add-data src/sourceworldbench_benchmarks/execution_tracer/bootstrap:sourceworldbench_benchmarks/execution_tracer/bootstrap \
    src/sourceworldbench_benchmarks/cli.py

# Fail the build if the tracer bundle can't be staged from the frozen binary —
# guards the --add-data wiring above against silent drift.
RUN /src/dist/sourceworldbench-benchmarks selfcheck-trace-bundle

# Fail the build if libpython or any dependency .so requires glibc > 2.31.
RUN max=$(objdump -T /usr/local/lib/libpython3.13.so* $(find .venv -name '*.so*') 2>/dev/null \
        | grep -o 'GLIBC_2\.[0-9]*' | sort -t. -k2 -n | tail -1) \
    && echo "bundled glibc floor: ${max}" \
    && [ -n "${max}" ] \
    && [ "$(printf '%s\nGLIBC_2.31\n' "${max}" | sort -t. -k2 -n | tail -1)" = "GLIBC_2.31" ]

FROM debian:bookworm-slim

COPY --from=build /src/dist/sourceworldbench-benchmarks /opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks
RUN chmod +x /opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks

ENTRYPOINT ["/opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks"]
