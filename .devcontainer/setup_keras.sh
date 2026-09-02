#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating directory for clones"
cd ..
sudo mkdir -p clones
sudo chown vscode:vscode clones/
cd clones

# Shared, host-persisted pip cache dir: bind-mounted into all three keras-dev
# containers so downloaded wheels/sdists (tensorflow-cpu, torch, jax, etc. are
# large) survive container recreation instead of being re-downloaded each time.
mkdir -p pip-cache/keras
PIP_CACHE_HOST_DIR="$(pwd)/pip-cache/keras"

echo "Cleaning any existing keras-dev containers"
docker rm -f keras-dev1 || true
docker rm -f keras-dev2 || true
docker rm -f keras-dev3 || true

mkdir -p clone1
cd clone1

echo "Creating first clone of keras (pre_version + post_version checkouts)"
mkdir -p pre_version post_version
rm -rf pre_version/keras post_version/keras
git clone https://github.com/keras-team/keras.git pre_version/keras
cp -a pre_version/keras post_version/keras
echo "Building dev container for keras (first clone)"
docker run -t -d --name keras-dev1 \
    -v "${PWD}/pre_version/keras:/pre_version/keras" \
    -v "${PWD}/post_version/keras:/post_version/keras" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_keras_to_run_in_container.sh" keras-dev1:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_keras_in_container.sh" keras-dev1:/root/rename_keras_in_container.sh
docker exec keras-dev1 chmod +x /root/setup.sh /root/rename_keras_in_container.sh
docker exec keras-dev1 /root/setup.sh
echo "Done with first clone"

echo "Creating second clone of keras"
cd ..
mkdir -p clone2
cd clone2
mkdir -p pre_version post_version
rm -rf pre_version/keras post_version/keras
cp -a ../clone1/pre_version/keras pre_version/keras
cp -a ../clone1/post_version/keras post_version/keras
echo "Building dev container for keras (second clone)"
docker run -t -d --name keras-dev2 \
    -v "${PWD}/pre_version/keras:/pre_version/keras" \
    -v "${PWD}/post_version/keras:/post_version/keras" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_keras_to_run_in_container.sh" keras-dev2:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_keras_in_container.sh" keras-dev2:/root/rename_keras_in_container.sh
docker exec keras-dev2 chmod +x /root/setup.sh /root/rename_keras_in_container.sh
docker exec keras-dev2 /root/setup.sh
echo "Done with second clone"

echo "Creating third clone of keras"
cd ..
mkdir -p clone3
cd clone3
mkdir -p pre_version post_version
rm -rf pre_version/keras post_version/keras
cp -a ../clone1/pre_version/keras pre_version/keras
cp -a ../clone1/post_version/keras post_version/keras
echo "Building dev container for keras (third clone)"
docker run -t -d --name keras-dev3 \
    -v "${PWD}/pre_version/keras:/pre_version/keras" \
    -v "${PWD}/post_version/keras:/post_version/keras" \
    -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
    python:3.12
docker cp "$SCRIPT_DIR/setup_keras_to_run_in_container.sh" keras-dev3:/root/setup.sh
docker cp "$SCRIPT_DIR/rename_keras_in_container.sh" keras-dev3:/root/rename_keras_in_container.sh
docker exec keras-dev3 chmod +x /root/setup.sh /root/rename_keras_in_container.sh
docker exec keras-dev3 /root/setup.sh
echo "Done with third clone"

cd "$REPO_ROOT"
