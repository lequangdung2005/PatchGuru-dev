#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating directory for clones"
cd ..
sudo mkdir -p clones
sudo chown vscode:vscode clones/
cd clones

# Shared, host-persisted ccache dir: bind-mounted into all three pandas-dev
# containers so cache hits accumulate across container recreations and are
# shared between clone slots (ccache keys off preprocessed source content, not
# file path, so hits from one clone are valid for another).
mkdir -p ccache/pandas
CCACHE_HOST_DIR="$(pwd)/ccache/pandas"

# Shared, host-persisted pip cache dir: bind-mounted into all three pandas-dev
# containers so downloaded wheels/sdists survive container recreation instead
# of being re-downloaded on every rebuild.
mkdir -p pip-cache/pandas
PIP_CACHE_HOST_DIR="$(pwd)/pip-cache/pandas"

echo "Cleaning any existing pandas-dev containers"
docker rm -f pandas-dev1 || true
docker rm -f pandas-dev2 || true
docker rm -f pandas-dev3 || true

mkdir -p clone1
cd clone1

echo "Creating first clone of pandas (pre_version + post_version checkouts)"
mkdir -p pre_version post_version
rm -rf pre_version/pandas post_version/pandas
git clone https://github.com/pandas-dev/pandas.git pre_version/pandas
cp -a pre_version/pandas post_version/pandas
echo "Building dev container for pandas (first clone)"
docker run -t -d --name pandas-dev1 \
    -v "${PWD}/pre_version/pandas:/pre_version/pandas" \
    -v "${PWD}/post_version/pandas:/post_version/pandas" \
    -v "${CCACHE_HOST_DIR}:/opt/ccache/pandas" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_pandas_to_run_in_container.sh" pandas-dev1:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_pandas_in_container.sh" pandas-dev1:/root/rename_pandas_in_container.sh
docker exec pandas-dev1 chmod +x /root/setup.sh /root/rename_pandas_in_container.sh
docker exec pandas-dev1 /root/setup.sh
echo "Done with first clone"

echo "Creating second clone of pandas"
cd ..
mkdir -p clone2
cd clone2
mkdir -p pre_version post_version
rm -rf pre_version/pandas post_version/pandas
cp -a ../clone1/pre_version/pandas pre_version/pandas
cp -a ../clone1/post_version/pandas post_version/pandas
echo "Building dev container for pandas (second clone)"
docker run -t -d --name pandas-dev2 \
    -v "${PWD}/pre_version/pandas:/pre_version/pandas" \
    -v "${PWD}/post_version/pandas:/post_version/pandas" \
    -v "${CCACHE_HOST_DIR}:/opt/ccache/pandas" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_pandas_to_run_in_container.sh" pandas-dev2:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_pandas_in_container.sh" pandas-dev2:/root/rename_pandas_in_container.sh
docker exec pandas-dev2 chmod +x /root/setup.sh /root/rename_pandas_in_container.sh
docker exec pandas-dev2 /root/setup.sh
echo "Done with second clone"

echo "Creating third clone of pandas"
cd ..
mkdir -p clone3
cd clone3
mkdir -p pre_version post_version
rm -rf pre_version/pandas post_version/pandas
cp -a ../clone1/pre_version/pandas pre_version/pandas
cp -a ../clone1/post_version/pandas post_version/pandas
echo "Building dev container for pandas (third clone)"
docker run -t -d --name pandas-dev3 \
    -v "${PWD}/pre_version/pandas:/pre_version/pandas" \
    -v "${PWD}/post_version/pandas:/post_version/pandas" \
    -v "${CCACHE_HOST_DIR}:/opt/ccache/pandas" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_pandas_to_run_in_container.sh" pandas-dev3:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_pandas_in_container.sh" pandas-dev3:/root/rename_pandas_in_container.sh
docker exec pandas-dev3 chmod +x /root/setup.sh /root/rename_pandas_in_container.sh
docker exec pandas-dev3 /root/setup.sh
echo "Done with third clone"

cd "$REPO_ROOT"
