"""Загрузка конфига (TOML) и секретов (.env)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"
DEFAULT_ENV = PROJECT_ROOT / ".env"

AgentName = Literal["planner", "executor", "verifier"]


class ProviderConfig(BaseModel):
    name: str
    kind: Literal["fake", "openai_compat"]
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    # Уровень рассуждений для «думающих» моделей (Gemini 3: minimal/low/medium/high).
    # None — не передаём, модель берёт своё умолчание.
    reasoning_effort: str | None = None
    # Лимиты провайдера; 0 — нет лимита или он неизвестен.
    rpm: int = 0
    tpm: int = 0
    tpd: int = 0
    rpd: int = 0
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


class AwarenessConfig(BaseModel):
    name: str
    # Каким агентам changelog текущей версии добавляется в системный промпт.
    changelog_to: list[AgentName] = []


class ExperimentConfig(BaseModel):
    name: str = "main"
    # Отдельный эксперимент для fake-провайдера: его трассы никогда не смешиваются с реальными.
    fake_name: str = "fake"
    seed: int = 42
    task_pool: int = 30
    api_versions: list[str] = Field(
        default_factory=lambda: ["v1", "v2_contract", "v2_contract_stale", "v2_units", "v2_benign"]
    )
    awareness: list[str] = Field(default_factory=lambda: ["A0", "A1"])
    executor_max_steps: int = 8
    workers: int = 3
    order_seed: int = 7
    runs_dir: Path = Path("runs")
    cache_dir: Path = Path("cache")


class AppConfig(BaseModel):
    experiment: ExperimentConfig
    awareness: dict[str, AwarenessConfig]
    providers: dict[str, ProviderConfig]

    def provider(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise KeyError(f"Провайдер {name!r} не описан в конфиге: {sorted(self.providers)}")
        return self.providers[name]


def load_config(path: Path = DEFAULT_CONFIG, env_path: Path | None = DEFAULT_ENV) -> AppConfig:
    if env_path is not None and env_path.exists():
        load_dotenv(env_path, override=False)
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cfg = AppConfig(
        experiment=ExperimentConfig(**raw.get("experiment", {})),
        awareness={
            name: AwarenessConfig(name=name, **body)
            for name, body in raw.get("awareness", {}).items()
        },
        providers={
            name: ProviderConfig(name=name, **body)
            for name, body in raw.get("providers", {}).items()
        },
    )
    unknown = set(cfg.experiment.awareness) - set(cfg.awareness)
    if unknown:
        raise ValueError(f"Условия осведомлённости без описания в [awareness.*]: {sorted(unknown)}")
    # Относительные пути считаем от корня проекта, а не от текущей директории.
    exp = cfg.experiment
    if not exp.runs_dir.is_absolute():
        exp.runs_dir = PROJECT_ROOT / exp.runs_dir
    if not exp.cache_dir.is_absolute():
        exp.cache_dir = PROJECT_ROOT / exp.cache_dir
    return cfg
