#!/bin/bash
set -euo pipefail

# System packages: only what fetching/installing the conda toolchain needs.
# Everything else (C/C++ compilers, ninja, meson, ccache) comes from the
# pandas-dev conda env, mirroring the reference .devcontainer_good and our own
# scipy setup. Note: the pipeline's DockerExecutor routes pandas execution
# through `mamba activate pandas-dev` (via Config "conda_env"), so keep the
# Miniforge prefix at ${HOME}/conda == /root/conda here.
apt update
apt install -y curl ca-certificates git

git config --global --add safe.directory /pre_version/pandas
git config --global --add safe.directory /post_version/pandas

# Fetch tags so versioneer can determine a proper version for BOTH checkouts
# (avoids "0+unknown", which breaks version-sensitive resolution downstream).
for version_dir in /pre_version/pandas /post_version/pandas; do
    (cd "$version_dir" && git fetch --tags)
done

# Install Miniforge (conda-forge-first distribution), same layout as
# setup_scipy_to_run_in_container.sh.
wget -O /tmp/Miniforge3.sh \
    "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash /tmp/Miniforge3.sh -b -p "${HOME}/conda"
rm -f /tmp/Miniforge3.sh
source "${HOME}/conda/etc/profile.d/conda.sh"
source "${HOME}/conda/etc/profile.d/mamba.sh"

# pandas-dev env: strict/pinned versions come from pandas's own environment.yml
# (python 3.11, meson=1.2.1, meson-python=0.13.1, numpy<3, plus the full
# test/optional runtime closure -- pyarrow, ipython, tzdata, lxml, ...). The
# closure is what pipeline test drivers import at execution time, so building
# from env.yml is what fixes the missing-deps error_repair/truncation failures.
# env.yml is identical on the pre/post side for essentially every PR (it's
# build-tooling config, not package behavior), so the pre_version checkout's
# copy is used to create the single shared env both sides build against.
mamba env create -f /pre_version/pandas/environment.yml -y

# Beyond the env.yml closure the pipeline needs:
# - c-compiler/cxx-compiler: pandas's env.yml (unlike scipy's) has no explicit
#   compiler package; meson needs a C toolchain on PATH to build the wheels.
#   The conda-forge versions resolve from the same channel as the rest.
# - ccache: wraps the compilers so the pre/post rebuild (near-identical
#   sources) and repeated per-PR rebuilds hit cache; rename_pandas_in_container.sh
#   does the actual CC/CXX wrapping.
# - cloudpickle: used by the oracle's mutation-analysis machinery.
mamba install -y -n pandas-dev -c conda-forge c-compiler cxx-compiler ccache cloudpickle

# ccache keys on preprocessed source content, not file path/commit/mtime, so it
# hits across different PR checkouts and across the pre/post rebuild within one
# PR. The cache dir is bind-mounted from the host (set up in setup_pandas.sh),
# so hits accumulate across container recreations and clone slots.
mkdir -p /opt/ccache/pandas
mamba env config vars set -n pandas-dev \
    CCACHE_DIR=/opt/ccache/pandas \
    CCACHE_MAXSIZE=20G

mamba clean -y -a

# Real dual-package-version mechanism: build genuine pre_pandas / post_pandas
# renamed distributions from the two (initially identical) checkouts and install
# both into the pandas-dev env. Per-PR, the pipeline re-runs this rename step
# after checking out a PR's commits (DockerExecutor.rebuild_wheels_if_stale), so
# executions reflect the actual analyzed commits rather than a build-time
# snapshot. See rename_pandas_in_container.sh, ported from rename_scripts/pandas.sh.
mamba activate pandas-dev
/root/rename_pandas_in_container.sh