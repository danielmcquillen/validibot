#!/bin/sh
set -eu

# This helper exists for Docker-based commercial installs.
#
# Adding ``validibot_pro`` or ``validibot_enterprise`` to ``INSTALLED_APPS``
# only activates a package that is already present in the Python
# environment. Docker images need a separate build-time step that installs the
# purchased commercial wheel into the image's virtualenv before Django starts.
#
# This script is that installation step. It also centralizes the hardening
# checks for customer builds so we only accept exact pinned package references
# from the trusted Validibot package host.

TRUSTED_PYPI_HOST="pypi.validibot.com"

if [ "$#" -ne 1 ]; then
    echo "Usage: install-commercial-package.sh <python-bin>" >&2
    exit 1
fi

python_bin="$1"
commercial_package="${VALIDIBOT_COMMERCIAL_PACKAGE:-}"
private_index_url="https://${TRUSTED_PYPI_HOST}/simple/"

if [ -z "${commercial_package}" ]; then
    exit 0
fi

netrc_path="${HOME}/.netrc"
if [ ! -f "${netrc_path}" ]; then
    echo "A BuildKit-mounted ${netrc_path} is required for commercial installs." >&2
    exit 1
fi

if netrc_mode="$(stat -c '%a' "${netrc_path}" 2>/dev/null)"; then
    :
else
    netrc_mode="$(stat -f '%Lp' "${netrc_path}")"
fi
case "${netrc_mode}" in
    ?00|??00) ;;
    *)
        echo "${netrc_path} must not be accessible by group or other users." >&2
        exit 1
        ;;
esac

python3 - "${netrc_path}" "${TRUSTED_PYPI_HOST}" <<'PY'
from __future__ import annotations

import netrc
import sys

credentials = netrc.netrc(sys.argv[1]).authenticators(sys.argv[2])
if credentials is None or not credentials[0] or not credentials[2]:
    raise SystemExit(
        f"{sys.argv[1]} must contain login and password for {sys.argv[2]}.",
    )
PY

validate_trusted_index_url() {
    python3 - "$1" "${TRUSTED_PYPI_HOST}" <<'PY'
from __future__ import annotations

import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
trusted_host = sys.argv[2]

if url.scheme != "https":
    raise SystemExit("Private package index must use https.")

if url.hostname != trusted_host:
    raise SystemExit(
        f"Private package index must use host {trusted_host}.",
    )

if not url.path.startswith("/simple/"):
    raise SystemExit(
        "Private package index must point at the /simple/ index path.",
    )

if url.username is not None or url.password is not None:
    raise SystemExit("Private index URLs must not contain credentials.")
PY
}

install_from_index() {
    validate_trusted_index_url "${private_index_url}"

    uv pip install \
        --python "${python_bin}" \
        --index "${private_index_url}" \
        "${commercial_package}"
}

if printf '%s' "${commercial_package}" | grep -Eq '^validibot-(pro|enterprise)==[A-Za-z0-9][A-Za-z0-9._!+-]*$'; then
    install_from_index
    exit 0
fi

if python3 - "${commercial_package}" "${TRUSTED_PYPI_HOST}" <<'PY'
from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs
from urllib.parse import urlparse

package_ref = sys.argv[1]
trusted_host = sys.argv[2]

url = urlparse(package_ref)
sha256_values = parse_qs(url.fragment).get("sha256", [])

if url.scheme != "https":
    raise SystemExit(1)

if url.hostname != trusted_host:
    raise SystemExit(1)

if not url.path.startswith("/packages/"):
    raise SystemExit(1)

if url.username is not None or url.password is not None:
    raise SystemExit(1)

filename = url.path.rsplit("/", 1)[-1]
if not re.fullmatch(r"validibot_(pro|enterprise)-[^/]+\.whl", filename):
    raise SystemExit(1)

if len(sha256_values) != 1 or not re.fullmatch(r"[0-9a-f]{64}", sha256_values[0]):
    raise SystemExit(1)
PY
then
    uv pip install --python "${python_bin}" "${commercial_package}"
    exit 0
fi

cat >&2 <<'EOF'
VALIDIBOT_COMMERCIAL_PACKAGE must be one of:
  - validibot-pro==X.Y.Z
  - validibot-enterprise==X.Y.Z
  - https://pypi.validibot.com/packages/validibot_pro-X.Y.Z-...whl#sha256=<64 hex chars>
  - https://pypi.validibot.com/packages/validibot_enterprise-X.Y.Z-...whl#sha256=<64 hex chars>

Floating package names like "validibot-pro" are refused because they resolve
whatever version the package index serves at build time.
EOF
exit 1
