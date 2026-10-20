#!/bin/sh
# Run me. Asks for the YouTube URL, then shares it with VLC on the LAN.
# Extra arguments are passed through, e.g.:  ./start-linux-mac.sh <url> --quality 720p
cd "$(dirname "$0")" || exit 1

if command -v uv >/dev/null 2>&1; then
    exec uv run restream.py "$@"
fi

if [ ! -x .venv/bin/python ]; then
    echo "First run: setting up, this takes a minute..."
    if ! python3 -m venv .venv; then
        echo "Could not create a Python virtual environment."
        echo "Install Python 3 (on Debian/Ubuntu: sudo apt install python3-venv) or uv, then retry."
        rm -rf ./.venv
        exit 1
    fi
    .venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade streamlink || exit 1
fi
exec .venv/bin/python restream.py "$@"
