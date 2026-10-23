"""Прогон судьи по провалам эксперимента и метрики атрибуции.

Вердикты лежат по одному файлу на run_id в runs/<exp>/<provider>/judge/<setup_hash>/: повторный
запуск пропускает уже оценённые (возобновляемость при исчерпании TPD Groq).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mas_drift.infra.config import ProviderConfig
from mas_drift.infra.llm import LLMClient
from mas_drift.infra.trace import is_completed, read_trace
from mas_drift.judge.compress import MAX_ITEMS, MAX_TEXT, TOKEN_BUDGET, compress_trace, leaks
from mas_drift.judge.prompt import JUDGE_PROMPT_VERSION, judge_messages, parse_verdict
from mas_drift.judge.truth import ground_truth

# Эвристика «назван ли настоящий механизм» — по ключевым словам, на обоих языках.
ROOT_CAUSE_KEYWORDS = {
    "v2_units": re.compile(r"cent|цент|единиц|minor unit|×\s*100|x\s*100|\*\s*100|100\s*раз|в\s*100", re.I),
    "v2_contract": re.compile(r"num_nights|schema|схем|parameter|параметр", re.I),
    "v2_contract_stale": re.compile(r"num_nights|schema|схем|parameter|параметр", re.I),
}


def judge_setup(pcfg: ProviderConfig) -> dict:
    return {"provider": pcfg.name, "model": pcfg.model, "temperature": pcfg.temperature,
            "reasoning_effort": pcfg.reasoning_effort, "json_mode": pcfg.json_mode,
            "prompt_version": JUDGE_PROMPT_VERSION, "max_items": MAX_ITEMS, "max_text": MAX_TEXT,
            "token_budget": TOKEN_BUDGET}


def setup_hash(setup: dict) -> str:
    return hashlib.sha256(json.dumps(setup, sort_keys=True).encode()).hexdigest()[:10]


def failed_traces(traces_dir: Path) -> list[Path]:
    """Все завершённые трассы с проваленным прогоном (не только из полных блоков)."""
    out = []
    for path in sorted(Path(traces_dir).glob("*.jsonl")):
        if is_completed(path):
            end = read_trace(path)[-1]
            if not end["check"]["success"]:
                out.append(path)
    return out


def judge_one(trace: Path, llm: LLMClient, setup: dict) -> dict:
    records = read_trace(trace)
    start, end = records[0], records[-1]
    comp = compress_trace(records)
    leaked = leaks(comp.text, records)
    r = llm.chat(judge_messages(comp.text))
    verdict, parse_error = parse_verdict(r.content)
    return {
        "run_id": start["run_id"],
        # Поля ниже — для анализа; судья их не видел.
        "api_version": start["condition"]["api_version"],
        "awareness": start["condition"]["awareness"],
        "task_id": start["task_id"],
        "gold_kind": start["gold_kind"],
        "fail_reason": end["check"]["reason"],
        "truth": ground_truth(records),
        "judge": setup,
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_text": comp.text,
        "input_chars": len(comp.text),
        "input_est_tokens": comp.est_tokens,
        "input_text_limit": comp.text_limit,
        "input_leaks": leaked,
        "response": r.content,
        "message_extra": r.message_extra,
        "usage": r.usage.model_dump(),
        "latency_s": round(r.latency_s, 3),
        "cached": r.cached,
        "verdict": verdict.model_dump() if verdict else None,
        "parse_error": parse_error,
    }


def verdict_path(out_dir: Path, run_id: str) -> Path:
    return Path(out_dir) / f"{run_id}.json"


def save(result: dict, out_dir: Path) -> None:
    path = verdict_path(out_dir, result["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_results(out_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(out_dir).glob("*.json"))]


# --- метрики ---

@dataclass
class Scores:
    group: str
    n: int = 0             # оценено вердиктов
    n_truth: int = 0       # из них с истиной (в метрики точности)
    parse_fail: int = 0
    agent_ok: int = 0
    step_exact: int = 0
    step_pm1: int = 0
    downstream: int = 0    # винит verifier, а истина — executor
    cause_ok: int = 0      # root_cause называет механизм (эвристика)
    cause_n: int = 0       # для скольких применима эвристика
    conf_sum: float = 0.0
    tokens_sum: int = 0
    over_budget: int = 0   # фактический вход > TOKEN_BUDGET

    def add(self, res: dict) -> None:
        self.n += 1
        prompt_tokens = res["usage"]["prompt_tokens"]
        self.tokens_sum += prompt_tokens
        self.over_budget += int(prompt_tokens > TOKEN_BUDGET)
        v, t = res["verdict"], res["truth"]
        if v is None:
            self.parse_fail += 1
        else:
            self.conf_sum += v["confidence"]
        if t["status"] != "ok":
            return
        self.n_truth += 1
        if v is None:
            return  # неразобранный вердикт — ошибка по всем метрикам точности
        agent_ok = v["blamed_agent"] == t["agent"]
        self.agent_ok += agent_ok
        self.step_exact += agent_ok and v["blamed_step"] == t["step"]
        self.step_pm1 += agent_ok and abs(v["blamed_step"] - t["step"]) <= 1
        self.downstream += v["blamed_agent"] == "verifier" and t["agent"] == "executor"
        kw = ROOT_CAUSE_KEYWORDS.get(res["api_version"])
        if kw is not None:
            self.cause_n += 1
            self.cause_ok += bool(kw.search(v["root_cause"]))


def score(results: list[dict]) -> list[Scores]:
    groups: dict[str, Scores] = {}
    for res in results:
        key = f"{res['api_version']}:{res['awareness']}"
        groups.setdefault(key, Scores(key)).add(res)
    total = Scores("ВСЕ")
    for res in results:
        total.add(res)
    return [groups[k] for k in sorted(groups)] + [total]


def categories(results: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for res in results:
        if res["verdict"]:
            out[f"{res['api_version']}:{res['awareness']}"][res["verdict"]["error_category"]] += 1
    return out
