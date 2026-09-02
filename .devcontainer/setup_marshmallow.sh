#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Creating directory for clones"
cd ..
sudo mkdir -p clones
sudo chown vscode:vscode clones/
cd clones

echo "Cleaning any existing marshmallow-dev containers"
docker rm -f marshmallow-dev1 || true
docker rm -f marshmallow-dev2 || true
docker rm -f marshmallow-dev3 || true

mkdir -p clone1
cd clone1

echo "Creating first clone of marshmallow (pre_version + post_version checkouts)"
mkdir -p pre_version post_version
rm -rf pre_version/marshmallow post_version/marshmallow
git clone https://github.com/marshmallow-code/marshmallow.git pre_version/marshmallow
cp -a pre_version/marshmallow post_version/marshmallow
echo "Building dev container for marshmallow (first clone)"
docker run -t -d --name marshmallow-dev1 \
    -v "${PWD}/pre_version/marshmallow:/pre_version/marshmallow" \
    -v "${PWD}/post_version/marshmallow:/post_version/marshmallow" \
    python:3.12
# marshmallow is pure Python, so load_package() (OracleHelpers.py) isolates
# both /pre_version/marshmallow and /post_version/marshmallow into private
# sys.modules namespaces straight from disk at oracle-run time -- neither
# tree needs to be pip-installed itself. What DOES need to be present is
# marshmallow's own runtime dependency closure (e.g. packaging) in the
# container's global site-packages, so installing against either tree once
# (here, pre_version) satisfies that for the whole container.
docker exec -w /pre_version/marshmallow marshmallow-dev1 pip install -e '.[dev]'
docker exec -w /pre_version/marshmallow marshmallow-dev1 pip install coverage
echo "Done with first clone"

#####
echo "Creating second clone of marshmallow"
cd ..
mkdir -p clone2
cd clone2
mkdir -p pre_version post_version
rm -rf pre_version/marshmallow post_version/marshmallow
cp -a ../clone1/pre_version/marshmallow pre_version/marshmallow
cp -a ../clone1/post_version/marshmallow post_version/marshmallow
echo "Building dev container for marshmallow (second clone)"
docker run -t -d --name marshmallow-dev2 \
    -v "${PWD}/pre_version/marshmallow:/pre_version/marshmallow" \
    -v "${PWD}/post_version/marshmallow:/post_version/marshmallow" \
    python:3.12
docker exec -w /pre_version/marshmallow marshmallow-dev2 pip install -e '.[dev]'
docker exec -w /pre_version/marshmallow marshmallow-dev2 pip install coverage
echo "Done with second clone"

echo "Creating third clone of marshmallow"
cd ..
mkdir -p clone3
cd clone3
mkdir -p pre_version post_version
rm -rf pre_version/marshmallow post_version/marshmallow
cp -a ../clone1/pre_version/marshmallow pre_version/marshmallow
cp -a ../clone1/post_version/marshmallow post_version/marshmallow
echo "Building dev container for marshmallow (third clone)"
docker run -t -d --name marshmallow-dev3 \
    -v "${PWD}/pre_version/marshmallow:/pre_version/marshmallow" \
    -v "${PWD}/post_version/marshmallow:/post_version/marshmallow" \
    python:3.12
docker exec -w /pre_version/marshmallow marshmallow-dev3 pip install -e '.[dev]'
docker exec -w /pre_version/marshmallow marshmallow-dev3 pip install coverage
echo "Done with third clone"

cd "$REPO_ROOT"
