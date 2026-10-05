# Трассировка требований SynAPS-ProgramPlan 0.1.0

Уровень: эксперимент. Карта показывает, что код делает сейчас и чем это проверено; это основа будущей программы и методики испытаний, а не документ сдачи заказчику.

## Функциональные требования заказчика

| Требование | Код | Тесты | Документ | Чего нет |
|---|---|---|---|---|
| **F1.** Сводный план: загрузка и объединение планов ОКР, этапов и исполнителей в единую иерархию | `model.py`, `merge.py`, `io/mspdi.py`, `io/excel.py` | `test_mspdi_import_and_merge`, `test_plan_roundtrip_mspdi`, `test_excel_roundtrip`, `test_optional_columns_may_stay_empty`, `test_unknown_references_are_rejected` | [Форматы данных](data-format.md), [руководство §2–3](user-guide.md#2-подготовка-данных) | Primavera XER, MPP напрямую, 1С |
| **F2.** Последовательность работ, ключевые события, обязательные сроки, ограничения на перенос этапов | `compiler.py`, ядро `PrecedenceEdge`, `disrupt.py`, `calendar.py` | `test_generalised_relations[*]`, `test_max_lag_is_respected`, `test_milestone_closes_on_the_predecessor_finish_day`, `test_pinned_task_keeps_its_dates`, `test_impossible_deadline_is_proven_infeasible`, `test_done_and_in_progress_work`, `test_vacation_delays_named_person`, `test_decreed_years_have_247_working_days[*]` | [Методология §2–3](methodology.md#2-единая-шкала-времени) | Прерывание работы через выходные |
| **F3.** Перегрузки, пересечения в использовании общих ресурсов, работы с риском срыва сроков | `conflicts.py`, `quality.py`, `cpm.py`, `montecarlo.py` | `test_source_plan_stand_contention_and_broken_link`, `test_dangling_task_is_a_quality_warning`, `test_fixed_durations_match_the_chain`, `test_same_seed_same_quantiles` | [Справочник кодов](reference-codes.md#конфликты-исходных-планов), [методология §9](methodology.md#9-оценка-риска) | Подтверждение экспертом на данных заказчика; драйверы риска |
| **F4.** Несколько вариантов перераспределения с влиянием на сроки и загрузку | `scenarios.py`, `planner.py` | `test_stability_never_moves_work_earlier_than_approved`, `test_shared_stand_serialises_two_projects`, `test_greedy_is_heuristic_and_checked`, демо A–E | [Руководство §5](user-guide.md#5-построение-плана-и-вариантов), [методология §7](methodology.md#7-варианты-плана) | Набор решений из одного вызова ядра |
| **F5.** Интерактивный Гант, загрузка ресурсов, критические работы, отклонения, причины изменений | `report.py`, `report_template.html`, `explanations.py`, `io/mspdi.py` (экспорт), `api.py` | `test_export_writes_an_accepted_plan`, `test_export_refuses_a_plan_that_was_not_accepted`, `test_solve_rejects_an_impossible_deadline`, проверка отчёта в браузере (`AUDIT.md`) | [Руководство §6–8](user-guide.md#6-как-читать-отчёт) | Роли, журнал решений, база данных, правка мышью |

## Сквозные требования плана

| Инвариант | Как обеспечен | Чем проверен |
|---|---|---|
| Ни один план не попадает в отчёт или интерфейс без `outcome.ok` | Двойная проверка в `planner.py`; отчёт, экспорт, риск и HTTP отказываются от непринятого плана | `test_no_false_accepts_under_random_shifts`, `test_rejected_plan_is_not_a_risk_baseline`, `test_export_refuses_a_plan_that_was_not_accepted`, `test_solve_rejects_an_impossible_deadline` |
| `INFEASIBLE` — только при доказательстве | `_claim(..., proof)` и признак `restricted` | `test_impossible_deadline_is_proven_infeasible`, `test_unproven_infeasibility_is_not_claimed` |
| Независимая проверка не зависит от решателя | `checker.py` не импортирует компилятор, планировщик и ядро | `test_checker_stays_independent_of_the_solver_path` |
| Воспроизводимость | Детерминированный CP-SAT, хеши в доказательной базе | `test_same_seed_same_plan_hash`, `test_same_seed_same_quantiles` |
| SynAPS без форка, по коммиту | Зависимость по полному SHA | `test_synaps_pin_is_a_commit_and_matches_the_published_files` |
| Изменения ядра обратно совместимы | Рёбра предшествования — новая необязательная часть модели ядра | Тесты ядра SynAPS, включая `test_precedence_edges.py` |
| Данные заказчика обезличены, вне git, с происхождением | `provenance` в модели и доказательной базе; `out/` в `.gitignore` | Отчёт показывает происхождение; [данные и безопасность](security-and-data.md) |
| ИИ не создаёт ограничения и не меняет план; объяснения — из фактов | Объяснения собираются из причин итогового плана; языковая модель не используется | Код `explanations.py`; [методология §8](methodology.md#8-объяснения) |
| Нет утверждений вне реестра | `CLAIMS_REGISTRY.md`, `BANNED_CLAIMS.txt` | `scripts/lint_claims.py` в CI |

## Ожидаемый эффект заказчика

Как эффект будет измеряться на пилоте — в [пилоте и развитии](pilot-and-roadmap.md#5-ожидаемый-эффект-и-как-он-будет-измерен). На синтетике эффект не измеряется: он имеет смысл только в сравнении с текущим процессом заказчика.
