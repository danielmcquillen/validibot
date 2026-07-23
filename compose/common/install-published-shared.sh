#!/bin/sh
set -eu

# Host development resolves validibot-shared from the editable sibling checkout
# declared in ``tool.uv.sources``. Docker build contexts intentionally exclude
# sibling repositories, so images must install the exact public dependency pin
# from ``project.dependencies`` instead of following that local source override.

if [ "$#" -ne 2 ]; then
    echo "Usage: install-published-shared.sh <python-bin> <pyproject.toml>" >&2
    exit 1
fi

python_bin="$1"
project_file="$2"

shared_requirement="$(
    "${python_bin}" - "${project_file}" <<'PY'
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

project_file = Path(sys.argv[1])
project = tomllib.loads(project_file.read_text(encoding="utf-8"))
matches = [
    dependency
    for dependency in project["project"]["dependencies"]
    if dependency.startswith("validibot-shared")
]

if len(matches) != 1:
    raise SystemExit(
        "pyproject.toml must declare exactly one validibot-shared dependency.",
    )

requirement = matches[0]
if not re.fullmatch(r"validibot-shared==[A-Za-z0-9][A-Za-z0-9._+-]*", requirement):
    raise SystemExit(
        "validibot-shared must use an exact public version pin for Docker builds.",
    )

print(requirement)
PY
)"

uv pip install --python "${python_bin}" "${shared_requirement}"
