"""scripts/logtee.py — the backend's terminal output is also kept in a size-rotated file."""

import os
import pathlib
import subprocess
import sys

LOGTEE = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "logtee.py"


def test_tees_to_stdout_and_rotates(tmp_path):
    log = tmp_path / "logs" / "backend.log"
    lines = [f"line {i:03d} " + "x" * 40 + "\n" for i in range(30)]
    env = {**os.environ, "FRIDGE_LOG_MAX_BYTES": "500", "FRIDGE_LOG_BACKUPS": "2"}
    out = subprocess.run(
        [sys.executable, str(LOGTEE), str(log)],
        input="".join(lines).encode(),
        capture_output=True,
        env=env,
        check=True,
    ).stdout.decode()
    assert out == "".join(lines)  # the terminal copy is unchanged
    kept = [log.with_name("backend.log.2"), log.with_name("backend.log.1"), log]  # oldest -> newest
    assert all(p.exists() for p in kept)
    assert not log.with_name("backend.log.3").exists()  # only FRIDGE_LOG_BACKUPS old files
    text = "".join(p.read_text() for p in kept)
    assert "".join(lines).endswith(text) and text  # the newest output, in order, nothing torn
    assert all(p.stat().st_size < 600 for p in kept)
