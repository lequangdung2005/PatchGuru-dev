#!/usr/bin/env bash
# Rename scipy → pre_scipy / post_scipy for C-extension isolation.
#
# Containerized port of rename_scripts/scipy.sh (repo root), invoked by
# setup_scipy_to_run_in_container.sh as the final provisioning step for each
# scipy-dev{1,2,3} container. Ported verbatim -- do not simplify any of the
# rename/SONAME-fixup logic below, it is hard-won handling of pybind11 /
# C-extension singleton collisions between the pre_scipy and post_scipy
# builds sharing one interpreter process.
#
# Expects /pre_version/scipy and /post_version/scipy to be mounted.
# Produces and installs pre_scipy and post_scipy wheels.
#
# Must be executed with the scipy-dev conda env active (setup_scipy_to_run_in_container.sh
# runs `mamba activate scipy-dev` before invoking this script) so `pip`/`meson`/`ninja`
# resolve to the env's toolchain.
set -e

process_repo() {
    local PREFIX=$1
    local OLD_NAME="scipy"
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

    find . -depth -name "*$OLD_NAME*" ! -path "*/.git/*" | while read -r item; do
        dir=$(dirname "$item")
        base=$(basename "$item")
        new_base="${base//$OLD_NAME/$NEW_NAME}"
        mv "$item" "$dir/$new_base"
    done

    # scipy.optimize._highspy is a pybind11 extension whose C++ RTTI/type-registration
    # symbols keep default (non-hidden) visibility and are NOT touched by the
    # sed/mv rename pass above (it only renames text-level Python-visible names).
    # If pre_scipy and post_scipy are both imported into the SAME process, pybind11's
    # process-global type registry (looked up via a fixed ABI-tag string on `builtins`)
    # sees the identical mangled "ObjSense" type from both copies and raises
    # "generic_type: type 'ObjSense' is already registered!". pybind11 keys that
    # registry off PYBIND11_INTERNALS_VERSION (baked into the lookup string, and
    # guarded with #ifndef so it's safely overridable, unlike PYBIND11_INTERNALS_ID
    # which the header redefines unconditionally). Giving each renamed package's
    # build a distinct version number gives it a private internals registry, so
    # pre_scipy and post_scipy never see each other's registered types even when
    # imported in the same interpreter.
    local INTERNALS_VERSION
    if [ "$PREFIX" = "pre" ]; then
        INTERNALS_VERSION=900001
    else
        INTERNALS_VERSION=900002
    fi

    mkdir -p "/${PREFIX}_version/dist_${PREFIX}"
    # Wrap the conda toolchain's compilers (set by gcc_linux-64/gxx_linux-64/gfortran_linux-64
    # activation) with ccache. CCACHE_DIR is set on the scipy-dev env by
    # setup_scipy_to_run_in_container.sh, pointing at a host-mounted, container-lifetime-
    # persistent cache dir; content-based cache keys mean this hits across PR checkouts
    # that share unchanged files with a prior run, not just within one build.
    CC="ccache ${CC:-gcc}" \
    CXX="ccache ${CXX:-g++}" \
    FC="ccache ${FC:-gfortran}" \
    CXXFLAGS="${CXXFLAGS:-} -DPYBIND11_INTERNALS_VERSION=${INTERNALS_VERSION}" \
        pip wheel . -w "/${PREFIX}_version/dist_${PREFIX}" --no-build-isolation
}

# Meson's build system only renames files/symbols containing "scipy" (via the
# sed/mv pass above). Internal shared libraries like scipy/special/libsf_error_state.so
# don't contain "scipy" in their filename, so they keep the SAME filename and
# SONAME in both the pre_scipy and post_scipy builds. The dynamic linker
# deduplicates DT_NEEDED libraries by SONAME across the whole process, so the
# second package's extensions silently bind against the FIRST package's copy
# of that library and fail with "undefined symbol: <prefix>_scipy_<...>" for
# any symbol the second package's copy actually defines. Give each renamed
# package's copy of every such library a unique SONAME and repoint its
# dependents (via patchelf) so pre_scipy and post_scipy never share a loaded
# shared object.
fix_internal_shared_lib_sonames() {
    local PREFIX=$1
    local PKG_DIR
    PKG_DIR="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/${PREFIX}_scipy"

    find "$PKG_DIR" -name '*.so' ! -iname '*cpython*' | while read -r lib; do
        local libdir libname newname
        libdir=$(dirname "$lib")
        libname=$(basename "$lib")
        newname="lib${PREFIX}_scipy_${libname#lib}"

        mv "$lib" "$libdir/$newname"
        patchelf --set-soname "$newname" "$libdir/$newname"

        find "$PKG_DIR" -name '*.so' | while read -r dependent; do
            if readelf -d "$dependent" 2>/dev/null | grep -qF "[$libname]"; then
                patchelf --replace-needed "$libname" "$newname" "$dependent"
            fi
        done
    done
}

# Clear dist dirs completely - stale wheels from different versions cause pip conflicts.
# This is necessary when testing across multiple commits with different package versions.
rm -rf /pre_version/dist_pre /post_version/dist_post

process_repo "pre"
process_repo "post"

pip install /pre_version/dist_pre/pre_scipy*.whl /post_version/dist_post/post_scipy*.whl --force-reinstall

fix_internal_shared_lib_sonames "pre"
fix_internal_shared_lib_sonames "post"
