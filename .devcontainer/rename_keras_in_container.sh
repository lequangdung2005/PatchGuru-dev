#!/usr/bin/env bash
# Rename keras → pre_keras / post_keras for dual-version import isolation.
# Run inside the keras Docker container after the pre/post checkouts are
# mounted (see .devcontainer/setup_keras.sh / setup_keras_to_run_in_container.sh).
#
# Ported verbatim from rename_scripts/keras.sh -- keep the two in sync.
#
# (keras itself is pure Python, but the same import-resolution bug applies:
# meson/setuptools editable installs use a sys.meta_path finder that ignores
# sys.path, so a plain "import keras" always resolves to whichever version
# was last pip-installed regardless of process/sys.path tricks).
#
# Expects /pre_version/keras and /post_version/keras to be mounted.
# Produces and installs pre_keras and post_keras wheels.
set -e

process_repo() {
    local PREFIX=$1
    local OLD_NAME="keras"
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

    rm -rf build/ keras.egg-info/

    # keras's own ecosystem has several sibling PyPI packages that share the
    # "keras" substring by name (keras_hub, keras_nlp, keras_cv) plus a
    # standalone compat package "tf_keras" — a blind substring rename (as
    # used for pandas/sklearn/scipy) would corrupt real imports of those
    # external packages. Use a word-boundary match instead: "\b...\b" does
    # NOT match "keras" inside "keras_hub"/"tf_keras" (underscore is a word
    # character, so there's no boundary there), but DOES match standalone
    # "keras" in import paths, pyproject.toml package-dir keys, etc.
    # Known residual gap: "tf.keras" attribute access (e.g. tf.keras.saving)
    # still gets incorrectly renamed since "." is a non-word boundary too —
    # this only affects legacy TF-compat code paths, not exercised by most
    # oracle targets.
    find . -type d -name ".git" -prune -o -type f -exec grep -Il "$OLD_NAME" {} + | \
        xargs -r sed -i "s/\b${OLD_NAME}\b/${NEW_NAME}/g"

    # Use the SAME word-boundary rule as the content sed above for file/dir
    # names, not a blind substring replace. Blind substring replace previously
    # renamed the internal "_tf_keras" compat-shim directory to "_tf_pre_keras"
    # while the content pass (correctly, per \b) left "_tf_keras" references
    # untouched inside it — the mismatch broke
    # "from pre_keras import _tf_keras as _tf_keras" with an ImportError.
    # A word-boundary basename rename keeps both passes in agreement: it still
    # renames the top-level "keras" dir and the nested "keras" dir under
    # _tf_keras/ (both standalone-word occurrences), but leaves "_tf_keras"
    # itself untouched, just like the content pass does.
    find . -depth -name "*$OLD_NAME*" ! -path "*/.git/*" | while read -r item; do
        dir=$(dirname "$item")
        base=$(basename "$item")
        new_base=$(printf '%s' "$base" | sed "s/\b${OLD_NAME}\b/${NEW_NAME}/g")
        if [ "$new_base" != "$base" ]; then
            mv "$item" "$dir/$new_base"
        fi
    done

    mkdir -p "/${PREFIX}_version/dist_${PREFIX}"
    pip wheel . -w "/${PREFIX}_version/dist_${PREFIX}" --no-build-isolation
}

rm -rf /pre_version/dist_pre /post_version/dist_post

process_repo "pre"
process_repo "post"

pip install /pre_version/dist_pre/pre_keras*.whl /post_version/dist_post/post_keras*.whl --force-reinstall
