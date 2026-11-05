"""Every operational script starts and shows its help (catches import/argument errors)."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


@pytest.mark.parametrize("name", ["init_databases.py", "check_connections.py",
                                  "simulate_data.py", "mqtt_ingest.py", "run_api.py",
                                  "smoke_test.py"])
def test_script_help(name):
    result = subprocess.run([sys.executable, str(SCRIPTS / name), "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_simulate_to_stdout_without_any_service(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "simulate_data.py"), "--env-file",
         str(Path(__file__).parent.parent / ".env.example"), "--target", "stdout",
         "--backfill-hours", "0.05", "--no-realtime"],
        capture_output=True, text=True, timeout=60, cwd=SCRIPTS.parent)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) >= 7 and '"metric": "dose_rate"' in lines[0]
