#!/usr/bin/env bash
# Rename pandas → pre_pandas / post_pandas for C-extension isolation.
# Run inside the pandas Docker container after the pre/post checkouts are
# mounted (see .devcontainer/setup_pandas.sh / setup_pandas_to_run_in_container.sh).
#
# Ported verbatim from rename_scripts/pandas.sh -- keep the two in sync.
#
# Expects /pre_version/pandas and /post_version/pandas to be mounted.
# Produces and installs pre_pandas and post_pandas wheels.
set -e

process_repo() {
    local PREFIX=$1
    local OLD_NAME="pandas"
    local NEW_NAME="${PREFIX}_${OLD_NAME}"
    local MOUNTED_DIR="/${PREFIX}_version/${OLD_NAME}"

    local WORKSPACE="/tmp/workspace_${PREFIX}"
    local TARGET_DIR="$WORKSPACE/$NEW_NAME"

    if [ ! -d "$MOUNTED_DIR" ]; then
        echo "ERROR: Source not found at $MOUNTED_DIR" >&2
        return 1
    fi

    rm -rf "$WORKSPACE"
    mkdir -p "$WORKSPACE"
    cp -a "$MOUNTED_DIR" "$TARGET_DIR"

    cd "$TARGET_DIR"

    # Remove stale meson build artifacts copied from the source checkout.
    # Without this, meson reuses the old build directory on incremental builds and
    # can embed pre-patch Python source into the wheel even when the checkout is correct.
    rm -rf build/

    find . -type d -name ".git" -prune -o -type f -exec grep -Il "$OLD_NAME" {} + | xargs -r sed -i "s/$OLD_NAME/$NEW_NAME/g"

    find . -type f -exec grep -Il "from_${NEW_NAME}" {} + | xargs -r sed -i "s/from_${NEW_NAME}/from_pandas/g"
    find . -type f -exec grep -Il "to_${NEW_NAME}" {} + | xargs -r sed -i "s/to_${NEW_NAME}/to_pandas/g"

    find . -depth -name "*$OLD_NAME*" ! -path "*/.git/*" | while read -r item; do
        dir=$(dirname "$item")
        base=$(basename "$item")
        new_base="${base//$OLD_NAME/$NEW_NAME}"
        mv "$item" "$dir/$new_base"
    done

    mkdir -p "/${PREFIX}_version/dist_${PREFIX}"
    # CCACHE_DIR/CCACHE_MAXSIZE are set by setup_pandas_to_run_in_container.sh; wrap
    # the compilers meson invokes so the pre/post rebuild (near-identical sources)
    # and repeat container runs both hit cache.
    CC="ccache ${CC:-gcc}" \
    CXX="ccache ${CXX:-g++}" \
        pip wheel . -w "/${PREFIX}_version/dist_${PREFIX}" --no-build-isolation
}

# Clear dist dirs completely - stale wheels from different versions cause pip conflicts.
# This is necessary when testing across multiple commits with different package versions.
rm -rf /pre_version/dist_pre /post_version/dist_post

process_repo "pre"
process_repo "post"

pip install /pre_version/dist_pre/pre_pandas*.whl /post_version/dist_post/post_pandas*.whl --force-reinstall
