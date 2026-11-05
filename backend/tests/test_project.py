"""Project hygiene: .env.example is complete and loads; every script starts."""

import re
import subprocess
import sys

import pytest

from app.config import BASE_DIR, Settings

ENV = BASE_DIR / ".env.example"


def test_env_example_documents_every_setting():
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", ENV.read_text(), re.M))
    missing = []
    for name, f in Settings.model_fields.items():
        alias = f.validation_alias
        names = [str(a).upper() for a in alias.choices] if alias is not None else [name.upper()]
        if not documented & set(names):
            missing.append(name.upper())
    assert missing == []


def test_env_example_loads():
    s = Settings(_env_file=ENV)
    assert s.opengauss_dsn.get_secret_value().startswith("host=127.0.0.1 port=15432")
    assert s.kb_classifications == ["public", "internal"] and s.llm_provider == "none"


@pytest.mark.parametrize("script", ["init_db", "build_knowledge", "simulate", "ingest_mqtt",
                                    "check", "run"])
def test_script_help(script):
    r = subprocess.run([sys.executable, str(BASE_DIR / "scripts" / f"{script}.py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_simulate_to_stdout():
    r = subprocess.run([sys.executable, "scripts/simulate.py", "--env-file", str(ENV),
                        "--target", "stdout", "--hours", "0.05", "--no-live"],
                       capture_output=True, text=True, timeout=60, cwd=BASE_DIR)
    assert r.returncode == 0, r.stderr
    assert '"metric": "dose_rate"' in r.stdout


def test_build_knowledge_dry_run():
    r = subprocess.run([sys.executable, "scripts/build_knowledge.py", "--env-file", str(ENV),
                        "--dry-run"], capture_output=True, text=True, timeout=60, cwd=BASE_DIR)
    assert r.returncode == 0, r.stderr
    assert "dose-rate-units:" in r.stdout and "sample_2026_11_05" in r.stdout
