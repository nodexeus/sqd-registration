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


# --- the ./sqd wrapper ------------------------------------------------------

WRAPPER = os.path.join(REPO, "sqd")


def run_wrapper(tmp_path, args, api_key="stub-key"):
    """Run ./sqd with a stub proxy that just reports its arguments."""
    stub = tmp_path / "stub-proxy"
    stub.write_text('#!/usr/bin/env bash\necho "PROXY_ARGS: $*"\n')
    stub.chmod(0o755)
    env = dict(os.environ, SQD_PROXY=str(stub))
    env.pop("FIREBLOCKS_API_KEY", None)
    if api_key:
        env["FIREBLOCKS_API_KEY"] = api_key
    return subprocess.run(
        [WRAPPER, *args], capture_output=True, text=True, env=env, cwd=REPO
    )


def test_the_wrapper_runs_under_bash_3_2(tmp_path):
    """macOS ships bash 3.2, where "${empty[@]}" under set -u is an error.

    An earlier version conditionally emptied an array and died with
    'signer_args[@]: unbound variable' before running anything.
    """
    run = run_wrapper(tmp_path, ["peers.txt", "--network", "tethys"])

    assert "unbound variable" not in run.stderr
    assert run.returncode == 0, run.stderr


def test_the_wrapper_adds_the_signer_flag_once(tmp_path):
    run = run_wrapper(tmp_path, ["peers.txt", "--network", "tethys"])

    assert run.stdout.count("--signer") == 1
    assert "--signer fireblocks" in run.stdout


def test_the_wrapper_does_not_duplicate_a_supplied_signer(tmp_path):
    run = run_wrapper(
        tmp_path, ["--signer", "fireblocks", "peers.txt", "--network", "tethys"]
    )

    assert run.stdout.count("--signer") == 1


def test_the_wrapper_passes_the_chain_id_matching_the_network(tmp_path):
    """Read from the same networks table, so the two cannot drift apart."""
    assert "chainId 421614" in run_wrapper(
        tmp_path, ["peers.txt", "--network", "tethys"]
    ).stdout
    assert "chainId 42161" in run_wrapper(
        tmp_path, ["peers.txt", "--network", "mainnet"]
    ).stdout


def test_the_wrapper_defaults_to_mainnet(tmp_path):
    assert "chainId 42161 " in run_wrapper(tmp_path, ["peers.txt"]).stdout


def test_an_unknown_network_fails_before_starting_the_proxy(tmp_path):
    run = run_wrapper(tmp_path, ["peers.txt", "--network", "nope"])

    assert run.returncode != 0
    assert "PROXY_ARGS" not in run.stdout


def test_without_a_key_the_wrapper_signs_locally(tmp_path):
    """No FIREBLOCKS_API_KEY means no proxy and no --signer flag."""
    if os.path.exists(os.path.join(REPO, "fireblocks.env")):
        pytest.skip("fireblocks.env present, so the wrapper routes remotely")
    run = run_wrapper(tmp_path, ["--help"], api_key=None)

    assert "PROXY_ARGS" not in run.stdout
    assert "usage: bulk_register.py" in run.stdout
