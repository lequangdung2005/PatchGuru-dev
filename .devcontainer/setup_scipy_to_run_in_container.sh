#!/bin/bash

apt update
# patchelf: needed by rename_scipy_in_container.sh's fix_internal_shared_lib_sonames()
# (SONAME rewriting so pre_scipy/post_scipy .so files don't collide when both are
# loaded into the same interpreter).
apt install -y gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config patchelf

wget -O Miniforge3.sh "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash Miniforge3.sh -b -p "${HOME}/conda"
source "${HOME}/conda/etc/profile.d/conda.sh"
source "${HOME}/conda/etc/profile.d/mamba.sh"
mamba shell init

# environment.yml is identical on the pre/post side for essentially every PR (it's
# build-tooling config, not package behavior), so the pre_version checkout's copy is
# used to create the single shared scipy-dev env both sides build against.
mamba env create -f /pre_version/scipy/environment.yml -y
mamba install -y -n scipy-dev -c conda-forge ccache
mamba activate scipy-dev

# ccache keys on preprocessed source content, not file path/commit/mtime, so it hits
# across different PR checkouts (most translation units are unchanged between PRs)
# and across the pre/post rebuild within one PR (near-identical sources). The cache
# dir is bind-mounted from the host in setup_scipy.sh, so hits accumulate across
# container recreations too. rename_scipy_in_container.sh wraps CC/CXX/FC with ccache
# for the actual build.
mkdir -p /opt/ccache/scipy
mamba env config vars set -n scipy-dev \
    CCACHE_DIR=/opt/ccache/scipy \
    CCACHE_MAXSIZE=20G
mamba deactivate
mamba activate scipy-dev

# Real dual-package-version install: build genuine pre_scipy / post_scipy distributions
# (C-extension rename, since sys.modules-isolation tricks can't isolate C-extension
# singletons) and install both into this same conda env, instead of a single editable
# `pip install -e .` against one checkout.
bash /root/rename_scipy_in_container.sh
