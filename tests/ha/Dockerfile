# The test image: the official Home Assistant image of the release the integration targets (2026.9.2, Python 3.14)
# and the test harness made for exactly that release. Nothing of the repo is copied in: it is mounted at /repo.
# tests/ha/test.sh builds it and runs the tests in it. By hand, Linux or WSL2, from the repo's root; --rm removes the
# container after the run:
#
#   docker build -t renac-ha-test:2026.9.2 tests/ha
#   docker run --rm --network host -v "$PWD":/repo renac-ha-test:2026.9.2
#
# The run is tests/ha/run.sh; its arguments are pytest's.
#
# Another release: change both versions. Inside that release's image,
# `pip install --dry-run pytest-homeassistant-custom-component homeassistant==<release>` names the harness to take.
FROM ghcr.io/home-assistant/home-assistant:2026.9.2

RUN pip install --no-cache-dir pytest-homeassistant-custom-component==0.13.365

# No bytecode written into the mounted repo. The image's own rich package warns on Python 3.14 (`return` in a
# `finally`) at every start; ruff and the Python 3.12 runs check this repo's code for the same.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONWARNINGS=ignore::SyntaxWarning
WORKDIR /repo
ENTRYPOINT ["sh", "tests/ha/run.sh"]
