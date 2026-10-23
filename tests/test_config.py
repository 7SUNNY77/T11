import os

from mas_drift.infra.config import DEFAULT_CONFIG, load_config, load_dotenv


def test_default_config_loads():
    cfg = load_config(DEFAULT_CONFIG, env_path=None)
    assert set(cfg.providers) == {"fake", "gemini", "groq", "ollama"}
    assert cfg.provider("fake").kind == "fake"
    assert cfg.experiment.task_pool == 30
    assert cfg.experiment.runs_dir.is_absolute()


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# c\nMASX_A="quoted"\nMASX_B=plain\n', encoding="utf-8")
    monkeypatch.setenv("MASX_B", "from_env")
    monkeypatch.delenv("MASX_A", raising=False)
    load_dotenv(env)
    assert os.environ["MASX_A"] == "quoted"
    assert os.environ["MASX_B"] == "from_env"
    monkeypatch.delenv("MASX_A")
