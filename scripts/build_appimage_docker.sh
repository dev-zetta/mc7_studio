#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image=${MC7_APPIMAGE_BUILD_IMAGE:-mc7-studio-appimage:ubuntu-22.04}
build_release=0
require_tag=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --build-release)
            build_release=1
            ;;
        --require-tag)
            require_tag=1
            ;;
        *)
            echo "Usage: $0 [--build-release] [--require-tag]" >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$require_tag" -eq 1 ] && [ "$build_release" -ne 1 ]; then
    echo "--require-tag requires --build-release" >&2
    exit 2
fi

command -v docker >/dev/null 2>&1 || {
    echo "Docker is required to build the release AppImage." >&2
    exit 1
}

mkdir -p \
    "$project_root/tmp/appimage-docker-home" \
    "$project_root/tmp/appimage-docker-tmp"

if [ -e "$project_root/dist/appimage" ]; then
    echo "Refusing to replace existing output directory: $project_root/dist/appimage" >&2
    exit 1
fi
if [ "$build_release" -eq 1 ] && [ -e "$project_root/dist/release" ]; then
    echo "Refusing to replace existing output directory: $project_root/dist/release" >&2
    exit 1
fi
if [ "$build_release" -eq 1 ] && [ -e "$project_root/dist/sources" ]; then
    echo "Refusing to replace existing output directory: $project_root/dist/sources" >&2
    exit 1
fi

docker build \
    --file "$project_root/packaging/appimage/Dockerfile" \
    --tag "$image" \
    "$project_root"

update_information=${UPDATE_INFORMATION:-gh-releases-zsync|dev-zetta|swarm2|latest|MC7-Studio-*-x86_64.AppImage.zsync}
source_date_epoch=$(git -C "$project_root" log -1 --format=%ct)
container=

cleanup() {
    if [ -n "$container" ]; then
        docker rm --force "$container" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT HUP INT TERM

container=$(docker create \
    --env HOME=/workspace/tmp/appimage-docker-home \
    --env TMPDIR=/workspace/tmp/appimage-docker-tmp \
    --env PYTHONPYCACHEPREFIX=/workspace/tmp/pycache \
    --env SOURCE_DATE_EPOCH="$source_date_epoch" \
    --env BUILD_RELEASE="$build_release" \
    --env REQUIRE_TAG="$require_tag" \
    --env UPDATE_INFORMATION="$update_information" \
    --workdir /workspace \
    "$image" \
    bash -euc '
        mkdir -p "$HOME" "$TMPDIR" /workspace/tmp/pycache
        export PYTHONPATH=/workspace/src
        sh -n packaging/appimage/AppRun
        desktop-file-validate packaging/appimage/mc7-studio.desktop
        appstreamcli validate --no-net packaging/appimage/io.github.dev_zetta.MC7Studio.metainfo.xml
        python3.12 -m py_compile scripts/build_appimage.py scripts/build_corresponding_sources.py packaging/appimage/entrypoint.py
        python3.12 -m unittest -q tests.test_appimage_build tests.test_corresponding_sources tests.test_runtime
        if [ "$BUILD_RELEASE" = 1 ]; then
            if [ "$REQUIRE_TAG" = 1 ]; then
                # docker cp can preserve the host checkout ownership. Trust only
                # this build checkout in the container-private Git configuration.
                git config --global --add safe.directory /workspace
                python3.12 scripts/build_release.py --require-tag --output-dir dist/release
            else
                python3.12 scripts/build_release.py --output-dir dist/release
            fi
        fi
        python3.12 scripts/build_appimage.py --output-dir dist/appimage --maximum-glibc 2.35 --update-information "$UPDATE_INFORMATION"
        if [ "$BUILD_RELEASE" = 1 ]; then
            set -- dist/appimage/*.AppImage.components.json
            test "$#" -eq 1
            python3.12 scripts/build_corresponding_sources.py --component-manifest "$1" --output-dir dist/sources
        fi
    ')

if [ "$require_tag" -eq 1 ]; then
    docker cp "$project_root/.git" "$container:/workspace/.git"
fi

docker start --attach "$container"
mkdir -p "$project_root/dist"
docker cp "$container:/workspace/dist/appimage" "$project_root/dist/appimage"
if [ "$build_release" -eq 1 ]; then
    docker cp "$container:/workspace/dist/release" "$project_root/dist/release"
    docker cp "$container:/workspace/dist/sources" "$project_root/dist/sources"
fi
