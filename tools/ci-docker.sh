#!/usr/bin/env bash
# Run the Linux jobs of .github/workflows/ci.yml in local Docker images, for
# when hosted runners are unavailable. Usage: tools/ci-docker.sh [checkout]
set -euo pipefail

src=$(cd -- "${1:-$(dirname -- "$0")/..}" && pwd)
[[ -f "$src/third_party/tensormeld/src/tensormeld/__init__.py" ]] || {
    echo "run: git submodule update --init --depth 1 third_party/tensormeld" >&2
    exit 2
}
echo "== node tool, Python 3.10"
docker run --rm -v "$src:/src:ro" -w /src python:3.10-slim sh -c \
    'python -c "import py_compile, sys; [py_compile.compile(p, doraise=True, cfile=\"/tmp/c.pyc\") for p in sys.argv[1:]]" node/hyperloom-node node/*.py && echo {} | python node/hyperloom-node version'
status=0
for version in 3.11 3.12 3.13; do
    echo "== unit, Python $version"
    # A writable copy: tests create files beside the checkout.
    # Unprivileged, like the nodes: the non-lab service code refuses to run as root.
    docker run --rm --user 1000:1000 -e HOME=/tmp -v "$src:/ro:ro" python:"$version"-slim sh -c \
        'cp -a /ro /tmp/src && cd /tmp/src || exit 1
         status=0
         python -m unittest discover -s tests -t . || status=1
         python -m pip install -e ".[test]" && python -m pytest -q || status=1
         exit "$status"' || status=1
done
exit $status
