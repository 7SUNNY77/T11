"""Загрузка конфига (TOML) и секретов (.env)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"
DEFAULT_ENV = PROJECT_ROOT / ".env"


class ProviderConfig(BaseModel):
    name: str
    kind: Literal["fake", "openai_compat"]
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    rpm: int = 0
    cache: bool = True
    timeout_s: float = 60.0
    max_retries: int = 6
    backoff_base_s: float = 2.0
    backoff_max_s: float = 120.0

    def api_key(self) -> str:
        if not self.api_key_env:
            return ""
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"Провайдер {self.name}: переменная {self.api_key_env} не задана (.env)"
            )
        return key


class ExperimentConfig(BaseModel):
    name: str = "stage1"
    seed: int = 42
    task_pool: int = 30
    api_versions: list[str] = Field(
        default_factory=lambda: ["v1", "v2_contract", "v2_units", "v2_benign"]
    )
    awareness: list[str] = Field(default_factory=lambda: ["A0", "A1"])
    executor_max_steps: int = 8
    runs_dir: Path = Path("runs")
    cache_dir: Path = Path("cache")


class AppConfig(BaseModel):
    experiment: ExperimentConfig
    providers: dict[str, ProviderConfig]

    def provider(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise KeyError(f"Провайдер {name!r} не описан в конфиге: {sorted(self.providers)}")
        return self.providers[name]


def load_dotenv(path: Path = DEFAULT_ENV) -> None:
    """Минимальный парсер .env: KEY=VALUE, комментарии #, кавычки. Не перезаписывает окружение."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(path: Path = DEFAULT_CONFIG, env_path: Path | None = DEFAULT_ENV) -> AppConfig:
    if env_path is not None:
        load_dotenv(env_path)
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cfg = AppConfig(
        experiment=ExperimentConfig(**raw.get("experiment", {})),
        providers={
            name: ProviderConfig(name=name, **body)
            for name, body in raw.get("providers", {}).items()
        },
    )
    # Относительные пути считаем от корня проекта, а не от текущей директории.
    exp = cfg.experiment
    if not exp.runs_dir.is_absolute():
        exp.runs_dir = PROJECT_ROOT / exp.runs_dir
    if not exp.cache_dir.is_absolute():
        exp.cache_dir = PROJECT_ROOT / exp.cache_dir
    return cfg
