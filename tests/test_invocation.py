"""Guards against the two ways ./bulk_register.py is mis-invoked.

Both run the script as a subprocess, because the failures happen at import
time — before anything importable exists to unit test.
"""

import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "bulk_register.py")


def find_old_python():
    """An interpreter older than 3.10, if this machine has one."""
    for name in ("python3.9", "python3.8", "python3"):
        path = shutil.which(name)
        if not path:
            continue
        out = subprocess.run(
            [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            continue
        major, _, minor = out.stdout.strip().partition(".")
        if (int(major), int(minor)) < (3, 10):
            return path
    return None


def test_an_old_python_explains_itself_instead_of_a_type_error():
    """The reported failure: ./bulk_register.py picks the system python3.

    Without the guard this dies with `TypeError: unsupported operand type(s)
    for |` pointing at a dataclass field in peerids.py, which says nothing
    about the real cause.
    """
    old = find_old_python()
    if old is None:
        pytest.skip("no pre-3.10 interpreter on this machine")

    run = subprocess.run(
        [old, SCRIPT, "peers.txt", "--action", "status"],
        capture_output=True,
        text=True,
    )

    assert run.returncode == 2
    assert "Python 3.10 or newer" in run.stderr
    assert "TypeError" not in run.stderr
    # The suggested command echoes the arguments back, ready to paste.
    assert "--action status" in run.stderr


def test_a_missing_dependency_points_at_the_virtualenv(tmp_path):
    """The other mis-invocation: right Python, but outside the virtualenv."""
    stub = tmp_path / "sitecustomize.py"
    stub.write_text("")
    env = dict(os.environ)
    # An import path with none of the third-party packages on it.
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTHONNOUSERSITE"] = "1"

    run = subprocess.run(
        [sys.executable, "-S", SCRIPT, "peers.txt"],
        capture_output=True,
        text=True,
        env=env,
    )

    if run.returncode == 0:
        pytest.skip("dependencies still importable under -S")
    assert run.returncode == 2
    assert "virtualenv" in run.stderr
    assert "Traceback" not in run.stderr
