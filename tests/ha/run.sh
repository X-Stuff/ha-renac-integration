#!/bin/sh
# The tests inside the test image (tests/ha/Dockerfile), from the repo's root; tests/ha/test.sh starts it. Two pytest
# runs: the core's tests with the harness's plugin off, as it blocks every socket for every test, 127.0.0.1 included;
# then the HA glue's tests with it. Options go to both runs. Paths, from the repo's root, pick the runs: those under
# tests/ha go to the glue's run, the other ones to the core's; without paths both runs take all their tests. A run
# that selects no test (`-k` matching only the other run's) counts as passed. The exit code is 1 when a run failed.

# Ctrl+C: pytest stops and reports, then this script stops too instead of starting the next run.
trap 'exit 130' INT

core_paths=0
glue_paths=0
for arg in "$@"; do
    case "$arg" in
        tests | tests/ | ./tests | ./tests/) ;;
        tests/ha | tests/ha/* | ./tests/ha | ./tests/ha/*) glue_paths=1 ;;
        tests/* | ./tests/*) core_paths=1 ;;
    esac
done

# One pytest run, core or glue: the options, and the paths that belong to it or else its whole folder.
run() {
    kind=$1
    shift
    paths=0
    for arg in "$@"; do
        shift
        case "$arg" in
            tests | tests/ | ./tests | ./tests/) continue ;;
            tests/ha | tests/ha/* | ./tests/ha | ./tests/ha/*)
                [ "$kind" = glue ] || continue
                paths=1
                ;;
            tests/* | ./tests/*)
                [ "$kind" = core ] || continue
                paths=1
                ;;
        esac
        set -- "$@" "$arg"
    done
    if [ "$kind" = core ]; then
        [ $paths = 1 ] || set -- tests "$@"
        python3 -m pytest -p no:cacheprovider -p no:homeassistant --ignore=tests/ha "$@"
    else
        [ $paths = 1 ] || set -- tests/ha "$@"
        python3 -m pytest -p no:cacheprovider "$@"
    fi
}

status=0
if [ $core_paths = 1 ] || [ $glue_paths = 0 ]; then
    run core "$@" || [ $? = 5 ] || status=1
fi
if [ $glue_paths = 1 ] || [ $core_paths = 0 ]; then
    run glue "$@" || [ $? = 5 ] || status=1
fi
exit $status
