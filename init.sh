#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="./.venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="${SCRIPT_DIR}/requirements.txt"

#echo "==> Checking Python 3.10+"
#python3 -c "import sys; assert sys.version_info >= (3,10), f'Python 3.10+ required, found {sys.version}'" \
#    || { echo "Error: Python 3.10+ not found"; exit 1; }

echo "==> Creating virtual environment: ${VENV_DIR}"
python3.12 -m venv "${VENV_DIR}"

if [[ ! -f "${REQ}" ]]; then
    echo "==> requirements.txt not found — creating"
    cat > "${REQ}" <<'EOF'
pandas==3.0.3
tqdm==4.67.3
requests
pydantic-settings
EOF
fi

echo "==> Installing dependencies"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REQ}"
"${VENV_DIR}/bin/pip" install --quiet -e "${SCRIPT_DIR}"

echo "==> Compiling translation catalogs (.po -> .mo)"
for po in "${SCRIPT_DIR}"/po/*.po; do
    [[ -e "$po" ]] || continue
    lang="$(basename "${po%.po}")"
    "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/tools/msgfmt.py" "$po" \
        -o "${SCRIPT_DIR}/pyresolv/locale/${lang}/LC_MESSAGES/pyresolv.mo"
done

if [[ ! -f "${SCRIPT_DIR}/.env" && -f "${SCRIPT_DIR}/.env.example" ]]; then
    echo "==> .env not found — copying .env.example (fill in GRAYLOG__*/GUNTER__* before running collect/resolve)"
    cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
fi

echo ""
echo "Done. Run, e.g.:"
echo "  ${VENV_DIR}/bin/pyresolv --type trim -i input.csv -o trimmed.csv"
