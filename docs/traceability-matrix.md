# Трассировка требований SynAPS-ProgramPlan 0.1.0

Уровень: эксперимент. Карта показывает, что код делает сейчас и чем это проверено; это основа будущей программы и методики испытаний, а не документ сдачи заказчику.

## Функциональные требования заказчика

| Требование | Код | Тесты | Документ | Чего нет |
|---|---|---|---|---|
| **F1.** Сводный план: загрузка и объединение планов ОКР, этапов и исполнителей в единую иерархию | `model.py`, `merge.py`, `io/mspdi.py`, `io/xer.py`, `io/excel.py` | `test_mspdi_import_and_merge`, `test_plan_roundtrip_mspdi`, `test_two_projects_with_a_native_cross_link`, `test_merged_program_shares_the_engineer_and_solves`, `test_cli_imports_xer`, `test_excel_roundtrip`, `test_optional_columns_may_stay_empty`, `test_unknown_references_are_rejected` | [Форматы данных](data-format.md), [руководство §2–3](user-guide.md#2-подготовка-данных) | MPP напрямую, 1С, базовый план из XER |
| **F2.** Последовательность работ, ключевые события, обязательные сроки, ограничения на перенос этапов | `compiler.py`, ядро `PrecedenceEdge`, `disrupt.py`, `calendar.py` | `test_generalised_relations[*]`, `test_max_lag_is_respected`, `test_milestone_closes_on_the_predecessor_finish_day`, `test_pinned_task_keeps_its_dates`, `test_impossible_deadline_is_proven_infeasible`, `test_done_and_in_progress_work`, `test_vacation_delays_named_person`, `test_decreed_years_have_247_working_days[*]` | [Методология §2–3](methodology.md#2-единая-шкала-времени) | Прерывание работы на время закрытия ресурса; несколько режимов выполнения |
| **F3.** Перегрузки, пересечения в использовании общих ресурсов, работы с риском срыва сроков | `conflicts.py`, `quality.py`, `cpm.py`, `montecarlo.py`, лист `Risks` в `io/excel.py` | `test_source_plan_stand_contention_and_broken_link`, `test_dangling_task_is_a_quality_warning`, `test_fixed_durations_match_the_chain`, `test_zero_uncertainty_reproduces_the_accepted_plan`, `test_max_lag_link_keeps_its_minimum_lag_in_the_simulation`, `test_same_seed_same_quantiles`, тесты драйверов риска в `test_risk.py`, `test_risk_register_roundtrip` | [Справочник кодов](reference-codes.md#конфликты-исходных-планов), [методология §10](methodology.md#10-оценка-риска), [руководство §10](user-guide.md#10-риск-срыва) | Подтверждение экспертом на данных заказчика; корреляции рисков |
| **F4.** Несколько вариантов перераспределения с влиянием на сроки и загрузку | `scenarios.py`, `planner.py`, `edits.py` (варианты R) | `test_stability_never_moves_work_earlier_than_approved`, `test_shared_stand_serialises_two_projects`, `test_greedy_is_heuristic_and_checked`, `test_repair_adds_an_accepted_variant_and_journals_it`, демо A–E | [Руководство §5](user-guide.md#5-построение-плана-и-вариантов), [методология §7](methodology.md#7-варианты-плана) | Набор решений из одного вызова ядра |
| **F5.** Интерактивный Гант, загрузка ресурсов, критические работы, отклонения, причины изменений | `report.py`, `report_template.html`, `workbench.py`, `edits.py`, `journal.py`, `auth.py`, `explanations.py`, `io/mspdi.py` (экспорт), `api.py` | `test_moving_before_a_predecessor_is_a_hard_violation`, `test_harmless_move_is_clean`, `test_stable_repair_disturbs_only_what_the_edit_forces`, `test_local_workbench_serves_the_report_and_checks`, `test_roles_gate_decisions`, `test_restart_restores_journaled_variants_and_keeps_numbering`, `test_journal_chain_detects_edits`, `test_check_and_replan_with_moves`, `test_export_writes_an_accepted_plan`, проверка рабочего места в браузере (`AUDIT.md`) | [Руководство §6, §11–13](user-guide.md#11-рабочее-место-планировщика) | База данных; одновременная правка несколькими пользователями; корпоративный вход |

## Сквозные требования плана

| Инвариант | Как обеспечен | Чем проверен |
|---|---|---|
| Ни один план не попадает в отчёт или интерфейс без `outcome.ok` | Двойная проверка в `planner.py`; отчёт, рабочее место, экспорт, риск и HTTP отказываются от непринятого плана; правка проверяется тем же `checker.py`; вариант R публикуется только при `outcome.ok`; восстановленный после перезапуска вариант проверяется заново | `test_no_false_accepts_under_random_shifts`, `test_rejected_plan_is_not_a_risk_baseline`, `test_export_refuses_a_plan_that_was_not_accepted`, `test_solve_rejects_an_impossible_deadline`, `test_infeasible_repair_is_not_published`, `test_unknown_task_or_rejected_base_is_refused` |
| `INFEASIBLE` — только при доказательстве | `_claim(..., proof)` и признак `restricted` | `test_impossible_deadline_is_proven_infeasible`, `test_unproven_infeasibility_is_not_claimed`; PSPLIB j30: 472 вердикта `OPTIMAL` без единого расхождения с известным оптимумом |
| Независимая проверка не зависит от решателя | `checker.py` не импортирует компилятор, планировщик и ядро | `test_checker_stays_independent_of_the_solver_path` |
| Воспроизводимость | Детерминированный CP-SAT, хеши в доказательной базе, общие случайные числа в риске | `test_same_seed_same_plan_hash`, `test_same_seed_same_quantiles` |
| SynAPS без форка, по коммиту | Зависимость по полному SHA | `test_synaps_pin_is_a_commit_and_matches_the_published_files` |
| Изменения ядра обратно совместимы | Рёбра предшествования — новая необязательная часть модели ядра | Тесты ядра SynAPS, включая `test_precedence_edges.py` |
| Данные заказчика обезличены, вне git, с происхождением | `provenance` в модели и доказательной базе; `out/` в `.gitignore`; PSPLIB не хранится в репозитории | Отчёт показывает происхождение; [данные и безопасность](security-and-data.md) |
| ИИ не создаёт ограничения и не меняет план; объяснения — из фактов | Объяснения собираются из причин итогового плана; языковая модель не используется; правки делает только человек и проверяет `checker.py` | Код `explanations.py`, `edits.py`; [методология §8–9](methodology.md#8-объяснения) |
| Решения прослеживаемы | Журнал с хеш-цепочкой, пользователем, ролью, хешем плана и входа | `test_journal_chain_detects_edits`, `test_report_with_risk_and_journal_command` |
| Нет утверждений вне реестра | `CLAIMS_REGISTRY.md`, `BANNED_CLAIMS.txt` | `scripts/lint_claims.py` в CI |

## Ожидаемый эффект заказчика

Как эффект будет измеряться на пилоте — в [пилоте и развитии](pilot-and-roadmap.md#5-ожидаемый-эффект-и-как-он-будет-измерен). На синтетике эффект не измеряется: он имеет смысл только в сравнении с текущим процессом заказчика.
