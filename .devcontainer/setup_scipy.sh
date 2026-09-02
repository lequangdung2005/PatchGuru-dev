#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating directory for clones"
cd ..
sudo mkdir -p clones
sudo chown vscode:vscode clones/
cd clones
POOL_DIR="$(pwd)"

# Shared, host-persisted ccache dir: bind-mounted into all three scipy-dev
# containers so cache hits accumulate across container recreations and are
# shared between clone slots (ccache keys off preprocessed source content, not
# file path, so hits from one clone are valid for another).
mkdir -p ccache/scipy
CCACHE_HOST_DIR="${POOL_DIR}/ccache/scipy"

# Shared, host-persisted pip cache dir: bind-mounted into all three scipy-dev
# containers so downloaded wheels/sdists survive container recreation instead
# of being re-downloaded on every rebuild.
mkdir -p pip-cache/scipy
PIP_CACHE_HOST_DIR="${POOL_DIR}/pip-cache/scipy"

echo "Cleaning any existing scipy-dev containers"
docker rm -f scipy-dev1 || true
docker rm -f scipy-dev2 || true
docker rm -f scipy-dev3 || true

# ClonedRepoManager (patchguru/patchguru/utils/ClonedRepoManager.py) expects each
# pool slot to already hold TWO independent, ready-to-use git working trees --
# clone{i}/pre_version/scipy and clone{i}/post_version/scipy -- since it only
# resets/fetches/checks-out into them (git rm/reset/clean/fetch/checkout), it never
# does the initial `git clone` itself. So this script must create both trees for
# every slot before the manager (or any oracle run) touches them.
echo "Creating first clone of scipy (pre_version, clone1)"
mkdir -p clone1/pre_version
rm -rf clone1/pre_version/scipy clone1/post_version/scipy
git clone https://github.com/scipy/scipy.git clone1/pre_version/scipy
(cd clone1/pre_version/scipy && git submodule update --init)

echo "Duplicating clone1/pre_version into clone1/post_version"
mkdir -p clone1/post_version
sudo rsync -a --exclude 'build/' --exclude 'Miniforge3.sh' clone1/pre_version/scipy/ clone1/post_version/scipy/

for i in 2 3; do
    echo "Populating clone${i} (pre_version + post_version) from clone1"
    mkdir -p "clone${i}/pre_version" "clone${i}/post_version"
    rm -rf "clone${i}/pre_version/scipy" "clone${i}/post_version/scipy"
    sudo rsync -a --exclude 'build/' --exclude 'Miniforge3.sh' clone1/pre_version/scipy/ "clone${i}/pre_version/scipy/"
    sudo rsync -a --exclude 'build/' --exclude 'Miniforge3.sh' clone1/pre_version/scipy/ "clone${i}/post_version/scipy/"
done

for i in 1 2 3; do
    echo "Building dev container for scipy (clone${i})"
    docker run -t -d --name "scipy-dev${i}" \
        -v "${POOL_DIR}/clone${i}/pre_version/scipy:/pre_version/scipy" \
        -v "${POOL_DIR}/clone${i}/post_version/scipy:/post_version/scipy" \
        -v "${CCACHE_HOST_DIR}:/opt/ccache/scipy" \
        -v "${PIP_CACHE_HOST_DIR}:/root/.cache/pip" \
        python:3.12
    docker cp "$SCRIPT_DIR/setup_scipy_to_run_in_container.sh" "scipy-dev${i}:/root/setup.sh"
    docker cp "$SCRIPT_DIR/rename_scipy_in_container.sh" "scipy-dev${i}:/root/rename_scipy_in_container.sh"
    docker exec "scipy-dev${i}" chmod +x /root/setup.sh /root/rename_scipy_in_container.sh
    docker exec -w /root "scipy-dev${i}" /root/setup.sh
    echo "Done with clone${i}"
done

cd "$REPO_ROOT"
