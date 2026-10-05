#!/bin/sh
# Runs the tests in Docker, in the test image (tests/ha/Dockerfile), building it first when it is missing or the
# Dockerfile changed. Linux or WSL2, from any folder; the arguments are pytest's, paths from the repo's root:
#
#   tests/ha/test.sh                                every test
#   tests/ha/test.sh -x -q                          every test, pytest's options
#   tests/ha/test.sh tests/test_poller.py           one file
#   tests/ha/test.sh tests/ha -k config_flow        the glue's tests whose names match
#
# The container is removed after the run (--rm). The exit code is the tests'.
set -e
repo=$(cd "$(dirname "$0")/../.." && pwd)
image=renac-ha-test:2026.9.2

old=$(docker images -q "$image")
if [ -z "$old" ]; then
    echo "Building the test image $image, once; it downloads Home Assistant's image."
    docker build -t "$image" "$repo/tests/ha"
else
    docker build -q -t "$image" "$repo/tests/ha" >/dev/null
    new=$(docker images -q "$image")
    # A rebuild leaves the image it replaced without a name.
    if [ "$old" != "$new" ]; then docker rmi "$old" >/dev/null || true; fi
fi

# On a terminal: pytest's colours, and Ctrl+C reaches the tests.
terminal=""
if [ -t 0 ] && [ -t 1 ]; then terminal="-it"; fi
exec docker run --rm $terminal --network host -v "$repo":/repo "$image" "$@"
