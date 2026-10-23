import os

import pytest

from mas_drift.infra.config import DEFAULT_CONFIG, load_config


def test_default_config_loads():
    cfg = load_config(DEFAULT_CONFIG, env_path=None)
    assert set(cfg.providers) == {"fake", "gemini", "groq", "ollama"}
    assert cfg.provider("fake").kind == "fake"
    assert cfg.provider("gemini").model == "gemini-3.5-flash-lite"
    assert cfg.provider("groq").tpd == 200000
    assert cfg.experiment.task_pool == 30
    assert cfg.experiment.runs_dir.is_absolute()
    assert cfg.awareness["A0"].changelog_to == []
    assert cfg.awareness["A1"].changelog_to == ["executor"]


def test_awareness_is_config_only(tmp_path):
    text = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        'awareness = ["A0", "A1"]', 'awareness = ["A0", "A1", "A2"]'
    ) + '\n[awareness.A2]\nchangelog_to = ["executor", "verifier"]\n'
    path = tmp_path / "c.toml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path, env_path=None)
    assert cfg.awareness["A2"].changelog_to == ["executor", "verifier"]


def test_undeclared_awareness_rejected(tmp_path):
    text = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        'awareness = ["A0", "A1"]', 'awareness = ["A0", "A9"]'
    )
    path = tmp_path / "c.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="A9"):
        load_config(path, env_path=None)


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# c\nMASX_A="quoted"\nMASX_B=plain\n', encoding="utf-8")
    monkeypatch.setenv("MASX_B", "from_env")
    monkeypatch.delenv("MASX_A", raising=False)
    load_config(DEFAULT_CONFIG, env_path=env)
    assert os.environ["MASX_A"] == "quoted"
    assert os.environ["MASX_B"] == "from_env"
    monkeypatch.delenv("MASX_A")
