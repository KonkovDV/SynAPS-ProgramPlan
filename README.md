# SynAPS-ProgramPlan

Контур сводного плана программы ОКР поверх [SynAPS](https://github.com/KonkovDV/SynAPS): несколько проектов, общие люди и стенды, связи FS/SS/FF/SF, директивные сроки, проверка и объяснение сдвигов.

Уровень 0.1.0 — эксперимент (ISO 16290 TRL 4, синтетические программы). Оценка по ГОСТ Р 58048 не проводилась. Что можно и чего нельзя утверждать — в `CLAIMS_REGISTRY.md`.

## Команды

```text
SynAPS-ProgramPlan demo --out-dir out/demo --time-limit 10
SynAPS-ProgramPlan synth --out program.json
SynAPS-ProgramPlan import program.xlsx --out program.json
SynAPS-ProgramPlan template --out program.xlsx
SynAPS-ProgramPlan analyze program.json
SynAPS-ProgramPlan solve program.json --out plan.json --explain
SynAPS-ProgramPlan scenarios program.json --out-dir out/scen --explain
SynAPS-ProgramPlan check program.json plan.json
SynAPS-ProgramPlan explain program.json plan.json
SynAPS-ProgramPlan witness program.json
SynAPS-ProgramPlan report program.json out/scen/plan_A.json --out report.html
SynAPS-ProgramPlan repair program.json plan.json --status-date 2026-11-02 --freeze-wd 10 --out plan2.json --out-program program2.json
SynAPS-ProgramPlan risk program.json plan.json --runs 200 --seed 42
SynAPS-ProgramPlan version
```

Код выхода: 0 — план принят или проверка чиста, 1 — принятого плана нет, 2 — неверные входные данные.

`demo` пишет `report.html`: сравнение вариантов, Гант, загрузка, причины переносов. Файл открывается без сети.

## Как устроен план

Рабочий день программы — одна минута ядра SynAPS. Веха — событие в день завершения предшественника; ядро не хранит нулевую длительность, адаптер компенсирует это и проверяет итог отдельно.

План публикуется, когда решатель нашёл допустимое решение, проверка SynAPS чиста и независимая доменная проверка не нашла жёстких нарушений.

Варианты: A сроки, B резерв ёмкости общих ресурсов, C минимум отклонений от утверждённых дат, D то же при сроке не хуже A более чем на 5%, E пересчёт изменённой программы.

## Проверка

```text
python -m pytest tests -q
python scripts/lint_claims.py
```

SynAPS подключается напрямую, без форка, и фиксируется коммитом `786cf1b3bc915941558e7e50b2935de684a32643` в `src/synaps_programplan/versions.py`, `pyproject.toml`, `CLAIMS_REGISTRY.md` и этом файле. Команда после установки: `SynAPS-ProgramPlan`. Из исходников: `python -m synaps_programplan`. HTTP-контур без хранения: `uvicorn synaps_programplan.api:app`. Карта требований: `docs/traceability-matrix.md`.
