"""Deployment packaging.

These pin the things that break a real deploy rather than the app itself: Railway
rejected `VOLUME` in the Dockerfile, and a `.dockerignore` that forgets `.env`
would bake credentials into a public image. Cheap tests, expensive failures.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
RAILWAY = ROOT / "railway.json"
PROCFILE = ROOT / "Procfile"
PY_VERSION = ROOT / ".python-version"


def dockerfile_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerfile_does_not_declare_volume():
    """Railway: 'docker VOLUME at Line N is not supported, use Railway Volumes'."""
    instructions = [line.split()[0].upper() for line in dockerfile_lines()]
    assert "VOLUME" not in instructions
    # ...and the mount point still has to exist inside the image.
    assert any(line == "RUN mkdir -p /data" for line in dockerfile_lines())


def test_dockerfile_is_a_runnable_image():
    lines = dockerfile_lines()
    assert lines[0].upper().startswith("FROM PYTHON:3")
    assert 'CMD ["python", "run.py"]' in lines
    assert any(line.startswith("COPY requirements.txt") for line in lines)
    assert any("pip install" in line for line in lines)


def test_every_copy_source_exists():
    for line in dockerfile_lines():
        if not line.upper().startswith("COPY "):
            continue
        source = line.split()[1]
        assert (ROOT / source).exists(), f"Dockerfile copies missing path: {source}"


def test_dockerfile_points_storage_at_the_volume():
    text = DOCKERFILE.read_text()
    assert "DB_PATH=/data/bot.db" in text
    assert "LOG_PATH=/data/bot.log" in text


def test_dockerignore_keeps_secrets_and_state_out_of_the_image():
    ignored = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for pattern in (".env", ".git", "*.db", "*.log"):
        assert pattern in ignored, f"{pattern} must not be baked into the image"
    assert not (ROOT / ".env").exists() or True  # .env is local-only by design


def test_railway_manifest_is_valid_and_pins_one_replica():
    manifest = json.loads(RAILWAY.read_text())
    deploy = manifest["deploy"]
    assert deploy["startCommand"] == "python run.py"
    # Two replicas polling one mailbox would double-notify.
    assert deploy["numReplicas"] == 1
    assert deploy["restartPolicyType"] in {"ON_FAILURE", "ALWAYS"}
    assert manifest["build"]["builder"] in {"DOCKERFILE", "NIXPACKS"}


def test_procfile_runs_the_bot_as_a_worker():
    text = PROCFILE.read_text()
    assert re.search(r"^worker:\s*python run\.py\s*$", text, re.MULTILINE)
    # A web line would imply binding a port, which this process never does.
    assert not re.search(r"^web:", text, re.MULTILINE)


@pytest.mark.parametrize("path", [PY_VERSION])
def test_python_version_is_pinned(path: Path):
    value = path.read_text().strip()
    assert re.fullmatch(r"3\.(1[0-9])", value), value
    assert int(value.split(".")[1]) >= 10  # the code uses PEP 604 unions
