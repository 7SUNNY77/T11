"""Сетка прогонов, run_id и порядок выполнения.

Порядок — блоками «задача × все условия»: блоки (задачи) перемешаны с фиксированным seed, внутри
блока условия тоже перемешаны. Если дневной лимит оборвёт прогон, готовые данные состоят почти
целиком из полных блоков — сбалансированы по условиям и парные по задачам, а дрейф провайдера
во времени не совпадает ни с одним условием.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from mas_drift.agents.pipeline import Condition
from mas_drift.agents.prompts import PROMPT_VERSION
from mas_drift.domain.tasks import Task
from mas_drift.infra.config import AppConfig, ProviderConfig
from mas_drift.tools.changelogs import changelog_for
from mas_drift.tools.versions import SPECS


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    condition: Condition
    task: Task
    repeat_idx: int
    block: int  # номер блока (задача × повтор) в порядке выполнения
    order: int  # сквозной номер прогона в порядке выполнения


def _sha(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def version_fingerprint(version: str) -> str:
    """Определение одной версии API (видимые схемы, валидация, поля ответа) и её changelog.
    Правка версии даёт новые run_id только у её прогонов; добавление новой версии старые не трогает."""
    spec = SPECS[version]
    return _sha([
        spec.schemas, spec.hotel_args.model_json_schema(), spec.nights_param, spec.price_field,
        spec.cents, spec.extra_hotel_fields, changelog_for(version),
    ])[:16]


def run_setup(pcfg: ProviderConfig, executor_max_steps: int, world_fp: str, tasks_fp: str) -> dict:
    """Всё, что определяет поведение прогона, кроме условия, задачи и повтора."""
    return {
        "provider": pcfg.name,
        "model": pcfg.model,
        "temperature": pcfg.temperature,
        "reasoning_effort": pcfg.reasoning_effort,
        "prompt_version": PROMPT_VERSION,
        "executor_max_steps": executor_max_steps,
        "world_fp": world_fp,
        "tasks_fp": tasks_fp,
    }


def make_run_id(setup: dict, cond: Condition, task_id: str, repeat_idx: int) -> str:
    h = _sha([setup, cond.model_dump(), version_fingerprint(cond.api_version), task_id, repeat_idx])[:10]
    return f"{cond.api_version}_{cond.awareness}_{task_id}_r{repeat_idx}_{h}"


def conditions_from_config(cfg: AppConfig, selection: str = "all") -> list[Condition]:
    exp = cfg.experiment
    all_conds = [
        Condition(api_version=v, awareness=a, changelog_to=cfg.awareness[a].changelog_to)
        for v in exp.api_versions
        for a in exp.awareness
    ]
    if selection == "all":
        return all_conds
    wanted = [s.strip() for s in selection.split(",") if s.strip()]
    by_id = {c.id: c for c in all_conds}
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise ValueError(f"Неизвестные условия {unknown}; доступны: {list(by_id)}")
    return [by_id[w] for w in wanted]


def build_grid(
    conditions: list[Condition],
    tasks: list[Task],
    repeats: int,
    setup: dict,
    order_seed: int,
) -> list[RunSpec]:
    rng = random.Random(order_seed)
    specs: list[RunSpec] = []
    block = 0
    for rep in range(repeats):
        task_order = list(tasks)
        rng.shuffle(task_order)
        for task in task_order:
            conds = list(conditions)
            rng.shuffle(conds)
            for cond in conds:
                specs.append(RunSpec(
                    run_id=make_run_id(setup, cond, task.task_id, rep),
                    condition=cond, task=task, repeat_idx=rep, block=block, order=len(specs),
                ))
            block += 1
    return specs


def experiment_dir(runs_dir: Path, exp_name: str, fake_name: str, pcfg: ProviderConfig) -> Path:
    """Fake-провайдер пишет только в эксперименты <fake_name>[-*], реальные — никогда туда."""
    is_fake_exp = exp_name == fake_name or exp_name.startswith(f"{fake_name}-")
    if pcfg.kind == "fake" and not is_fake_exp:
        raise ValueError(f"fake-провайдер пишет только в эксперимент {fake_name!r} (или {fake_name}-*)")
    if pcfg.kind != "fake" and is_fake_exp:
        raise ValueError(f"Эксперимент {exp_name!r} зарезервирован для fake-провайдера")
    return Path(runs_dir) / exp_name / pcfg.name
