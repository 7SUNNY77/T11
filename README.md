# Дрейф API инструментов в мультиагентной LLM-системе — этап 1

Каркас эксперимента: как изменения API инструментов влияют на МАС (Planner → Executor → Verifier)
и помогает ли агентам changelog. Сетка: версии API {v1, v2_contract, v2_units, v2_benign} ×
осведомлённость {A0 — без changelog, A1 — changelog в промпте Executor}.

## Установка

```bash
py -V:Astral/CPython3.11.15 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env   # и вписать ключи в .env (он в .gitignore; .env.example — только шаблон)
.venv/Scripts/python -m pytest
```

## Запуск

```bash
# fake-провайдер: проверка логики, пишет только в runs/fake/
python run.py --provider fake --conditions all --n-tasks 30 --repeats 1

# оценка бюджета без вызовов (берёт задержку и токены из реальных трасс, если они есть)
python run.py --provider gemini --estimate

# smoke: одна задача, подробный вывод шагов, без кэша
python scripts/smoke.py --provider gemini --task T01 --version v1 --awareness A0

# пилот: одна задача × все 8 условий, отдельный эксперимент
python run.py --provider gemini --tasks T01 --experiment pilot

# полная сетка
python run.py --provider gemini --conditions all --n-tasks 30 --repeats 1
```

Прочие флаги: `--conditions v1:A0,v2_units:A1`, `--workers N`, `--max-runs N`, `--experiment NAME`.

**Возобновление.** Прогон завершён, если есть `runs/<exp>/<provider>/traces/<run_id>.jsonl`
с записью `run_end`. Повторный запуск той же команды пропускает завершённые прогоны. При исчерпании
дневного лимита (429 «per day») или исчерпании повторов раннер не стартует новые прогоны,
дожидается идущих и выходит с кодом 2 — просто запустите ту же команду позже. Прерванные прогоны
остаются `.jsonl.part` (с `run_aborted`) и перезапускаются. HTTP 400/401/403/404 — ошибка
конфигурации (например, «User location is not supported»): прогон не засчитывается, сетка
останавливается.

## Конфиг (`config/default.toml`)

- `[experiment]`: seed мира и задач, пул задач (30), версии API, условия осведомлённости,
  `workers` (3), `order_seed`, лимит шагов Executor (8).
- `[awareness.<имя>] changelog_to = [...]` — каким агентам добавить changelog. Новое условие
  (например, `A2 = ["executor", "verifier"]`) — только правка конфига.
- `[providers.<имя>]`: `base_url`, `api_key_env`, `model`, `temperature`, `reasoning_effort`,
  лимиты `rpm`/`tpm`/`rpd`/`tpd` (0 — нет или неизвестен), `cache`.

`run_id` = `<версия>_<условие>_<задача>_r<повтор>_<хэш>`; хэш включает провайдера, модель,
temperature, reasoning_effort, версию промптов, отпечатки мира, задач и определения API.
Любое изменение этих параметров даёт новые прогоны, а не смешивание со старыми.

## Структура

| Путь | Что |
|---|---|
| `mas_drift/domain/` | генератор мира и задач, эталон: множество пар с минимальной суммой, ничьи, no_option |
| `mas_drift/tools/` | mock-API в 4 версиях, changelog'и, формулировки ошибок (`errors.py`), разметка дрейфа (`drift.py`) |
| `mas_drift/agents/` | промпты, Planner/Executor/Verifier, pipeline, PolicyFake |
| `mas_drift/eval/` | детерминированный checker |
| `mas_drift/infra/` | конфиг, LLM-клиенты, кэш, rate limiter + retry, трассы |
| `mas_drift/runner/` | сетка и порядок, параллельный запуск, results.csv, `--estimate` |

## Выходные данные

`runs/<exp>/<provider>/`: `traces/*.jsonl`, `results.csv`, `summary.csv`, `meta.json`, `world.json`, `tasks.json`.

**Трасса** (JSONL): `run_start` (условие, задача, эталон, модель, reasoning_effort, время начала UTC,
JSON-схемы инструментов) → `step` на каждый вызов LLM (agent, messages, ответ, tool_calls, токены,
задержка, cached, число HTTP-запросов) → `run_end` (финальный ответ, результат checker'а, счётчики).
Каждый результат инструмента в `tool_results` размечен служебными полями, которых агенты не видят:
`drift_affected`, `drift_type` и `shadow_v1_result` — ответ v1 на тот же вызов.

**results.csv** — одна строка на прогон: условие, задача, `gold_kind`, success, reason, `pair_ok`,
`total_ok`, `json_fenced`, статус Executor, LLM-вызовы, HTTP-запросы, кэш, токены (включая reasoning),
суммарная задержка, ошибки инструментов, число результатов с `drift_affected`, фактически
полученный changelog по каждому агенту (`none` / `own` / `irrelevant`), время начала.

## Особенности дизайна, которые важно помнить при анализе

- **Общий кэш между условиями.** Ключ кэша не содержит условия. Дословно одинаковые вызовы
  разных условий одной задачи и повтора получают один ответ. Planner, не получающий changelog,
  в одном блоке задачи видит одинаковый промпт во всех 8 условиях, значит план у них общий.
  Первый шаг Executor у v1 и v2_units с одинаковой осведомлённостью тоже совпадает: схемы
  идентичны. Это общие случайные числа: условия расходятся ровно там, где их меняет дрейф,
  и парное сравнение точнее. Но наблюдения в блоке не независимы. Одновременные промахи по одному
  ключу выполняются один раз, поэтому совместность не зависит от гонки воркеров.
- **Порядок прогонов** — блоками «задача × все условия», перемешанными с `order_seed`. Частичные
  данные сбалансированы по условиям и парные, а дрейф провайдера во времени не совпадает ни с одним
  условием.
- **v2_units и no_option.** Наивный агент видит цены ×100, считает, что всё дороже бюджета,
  и «угадывает» задачи без решения. Успех считайте отдельно по `gold_kind == "pairs"`.
- **PolicyFake** — только тест логики пайплайна, его результаты не являются данными.

## Gemini через OpenAI-совместимый endpoint

- `gemini-2.5-flash-lite` недоступна новым пользователям, используется `gemini-3.5-flash-lite`.
- Gemini 3 кладёт `thought_signature` в `tool_calls[i].extra_content.google`, при параллельных вызовах
  только в первый. Её надо вернуть в истории, иначе следующий запрос получит HTTP 400.
  Клиент возвращает все нестандартные поля tool_call дословно, а поля уровня сообщения пишет
  только в трассу.
- Gemini API недоступен из части регионов (HTTP 400 «User location is not supported»).
