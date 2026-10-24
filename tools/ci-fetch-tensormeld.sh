#!/usr/bin/env bash
# CI only: fetch the pinned TensorMeld submodule, which is a private repository.
# The default GITHUB_TOKEN cannot read it, so a fine-grained read-only token for
# that one repository is passed as TENSORMELD_TOKEN (repository secret of the
# same name). It is used only by this command, through a one-off insteadOf in
# git's environment (not on any command line, not written to any config file);
# actions/checkout keeps persist-credentials: false.
set -euo pipefail

if [[ -z "${TENSORMELD_TOKEN:-}" ]]; then
    echo "::error::The repository secret TENSORMELD_TOKEN is missing or empty. Create a fine-grained," \
         "read-only token (Contents: read) for the tensormeld repository and add it as the repository" \
         "secret TENSORMELD_TOKEN; CI cannot fetch the private third_party/tensormeld submodule without it." >&2
    exit 1
fi
echo "::add-mask::${TENSORMELD_TOKEN}"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="url.https://x-access-token:${TENSORMELD_TOKEN}@github.com/hyukudan/tensormeld.insteadOf"
export GIT_CONFIG_VALUE_0="https://github.com/hyukudan/tensormeld"
export GIT_TERMINAL_PROMPT=0
git submodule update --init --depth 1 third_party/tensormeld
