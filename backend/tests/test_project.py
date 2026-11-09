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


def test_env_example_has_no_comment_values():
    """`KEY=   # comment` makes python-dotenv read the comment as the value."""
    from dotenv import dotenv_values

    bad = {k: v for k, v in dotenv_values(ENV).items() if v and v.lstrip().startswith("#")}
    assert bad == {}


def test_env_example_loads():
    s = Settings(_env_file=ENV)
    assert s.opengauss_dsn.get_secret_value().startswith("host=127.0.0.1 port=15432")
    assert s.kb_classifications == ["public", "internal"] and s.llm_provider == "none"
    assert s.api_key is None and s.llm_base_url is None and s.mqtt_client_id is None


@pytest.mark.parametrize("script", ["init_db", "build_knowledge", "simulate", "ingest_mqtt",
                                    "analyze", "check", "run"])
def test_script_help(script):
    r = subprocess.run([sys.executable, str(BASE_DIR / "scripts" / f"{script}.py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_simulate_to_stdout():
    r = subprocess.run([sys.executable, "scripts/simulate.py", "--env-file", str(ENV),
                        "--target", "stdout", "--hours", "0.1", "--no-live"],
                       capture_output=True, text=True, timeout=60, cwd=BASE_DIR)
    assert r.returncode == 0, r.stderr
    assert '"metric": "dose_rate"' in r.stdout and '"metric": "battery_voltage"' in r.stdout


def test_build_knowledge_dry_run():
    r = subprocess.run([sys.executable, "scripts/build_knowledge.py", "--env-file", str(ENV),
                        "--dry-run"], capture_output=True, text=True, timeout=60, cwd=BASE_DIR)
    assert r.returncode == 0, r.stderr
    assert "dose-rate-units:" in r.stdout and "sample_2026_11_09" in r.stdout


def test_analyze_script_without_databases(tmp_path):
    """Memory mode: no IoTDB data -> every configured device is reported as missing data."""
    env = tmp_path / "env"
    env.write_text("TIMESERIES_BACKEND=memory\nANALYSIS_SCHEDULE_ENABLED=false\n")
    r = subprocess.run([sys.executable, "scripts/analyze.py", "--env-file", str(env)],
                       capture_output=True, text=True, timeout=120, cwd=BASE_DIR)
    assert r.returncode == 0, r.stderr
    assert "succeeded" in r.stdout and "no_data" not in r.stdout
    assert "分析窗口内无剂量率数据" in r.stdout and "ST03 dose_thresholds" in r.stdout
