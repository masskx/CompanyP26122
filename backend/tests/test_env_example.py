"""Keep .env.example complete and loadable."""

import re
from pathlib import Path

from industrial_ai.core.config import Settings

ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def _env_names(field_name: str, field) -> list[str]:
    alias = field.validation_alias
    if alias is not None and hasattr(alias, "choices"):
        return [str(c).upper() for c in alias.choices]
    return [field_name.upper()]


def test_every_setting_is_documented():
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.M))
    missing = [name for name, f in Settings.model_fields.items()
               if not any(n in documented for n in _env_names(name, f))]
    assert missing == [], f"add these to .env.example: {missing}"


def test_env_example_loads_as_is(monkeypatch):
    for name in ("DEEPSEEK_API", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=ENV_EXAMPLE)
    assert s.timeseries_backend == "iotdb" and s.session_store_backend == "opengauss"
    assert s.opengauss_dsn.get_secret_value().startswith("host=127.0.0.1 port=15432")
    assert s.kb_allowed_classifications == ["public", "internal"]
    assert s.mqtt_topics == ["sim/radiation/#"]
    assert s.llm_provider == "none" and s.embedding_provider == "none"
