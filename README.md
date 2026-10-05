# SynAPS-OKRPlan

Контур сводного плана программы ОКР поверх [SynAPS](https://github.com/KonkovDV/SynAPS): несколько проектов, общие люди и стенды, связи FS/SS/FF/SF, директивные сроки, проверка и объяснение сдвигов.

Уровень 0.1.0 — эксперимент (ISO 16290 TRL 4, синтетические программы). Оценка по ГОСТ Р 58048 не проводилась. Что можно и чего нельзя утверждать — в `CLAIMS_REGISTRY.md`.

## Команды

```text
okrplan demo --out-dir out/demo --time-limit 10
okrplan synth --out program.json
okrplan import a.xml b.xml --codes okr1 okr2 --links links.csv --out program.json
okrplan analyze program.json
okrplan solve program.json --out plan.json --explain
okrplan scenarios program.json --out-dir out/scen --explain
okrplan check program.json plan.json
okrplan explain program.json plan.json
okrplan witness program.json
okrplan report program.json out/scen/plan_A.json --out report.html
okrplan repair program.json plan.json --status-date 2026-11-02 --freeze-wd 10 --out plan2.json --out-program program2.json
okrplan version
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

SynAPS подключается напрямую и фиксируется коммитом в `src/synaps_okrplan/versions.py` и `pyproject.toml`.
