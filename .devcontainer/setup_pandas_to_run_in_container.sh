#!/bin/bash
set -e

apt update
apt install -y gcc git ninja-build libhdf5-dev libxml2-dev libxslt1-dev libpq-dev ccache

# ccache keys on preprocessed source content, not file path/commit/mtime, so it hits
# across different PR checkouts (most translation units are unchanged between PRs)
# and across the pre/post rebuild within one PR (near-identical sources). The cache
# dir is bind-mounted from the host in setup_pandas.sh, so hits accumulate across
# container recreations too. rename_pandas_in_container.sh wraps CC/CXX with ccache
# for the actual build.
mkdir -p /opt/ccache/pandas
export CCACHE_DIR=/opt/ccache/pandas
export CCACHE_MAXSIZE=20G

pip install \
    setuptools \
    'meson>=1.2.3,<2' \
    'meson-python>=0.19.0,<1' \
    'Cython>3.1.0,<4.0.0a0' \
    'numpy>=2.0.0' \
    wheel \
    'versioneer[toml]'

# Fetch tags so versioneer can determine a proper version for BOTH checkouts
# (avoids "0+unknown", which breaks version-sensitive resolution downstream).
for version_dir in /pre_version/pandas /post_version/pandas; do
    git config --global --add safe.directory "$version_dir"
    (cd "$version_dir" && git fetch --tags)
done

# Real dual-package-version mechanism: build genuine pre_pandas / post_pandas
# renamed distributions from the two checkouts and install both into this
# single container (C-extension singletons can't be isolated via sys.modules
# tricks, so both sides must be actually separate installed distributions).
# See rename_pandas_in_container.sh, ported from rename_scripts/pandas.sh.
/root/rename_pandas_in_container.sh

pip install 'tzdata>=2023.3'
