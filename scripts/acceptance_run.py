"""Write an unsigned protocol draft from pins and already measured benchmarks.

The draft records what the repository can show on this machine. Section 3 of
the test procedure stays unfilled: there is no anonymised customer file and no
run on the target operating system. The script does not change the maturity
level in versions.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = (
    "psplib_j30.json",
    "psplib_j60.json",
    "psplib_j120.json",
    "rcpspmax_j10.json",
    "rcpspmax_j20.json",
    "rcpspmax_j30.json",
    "scale.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-pytest", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, SYNAPS_COMMIT, VERSION

    pytest_line = "не запускался (--skip-pytest)"
    if not args.skip_pytest:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        tail = (completed.stdout or completed.stderr).strip().splitlines()
        pytest_line = (tail[-1] if tail else "нет вывода") + f"; код {completed.returncode}"
    lines = [
        "# Протокол испытаний",
        "",
        "Сводка автоматических проверок и прогонов на открытых наборах задач. Подписи ставятся на стенде.",
        "",
        "| Поле | Значение |",
        "|---|---|",
        f"| Объект | SynAPS-ProgramPlan {VERSION}, ядро `{SYNAPS_COMMIT}` |",
        "| Контур | машина, на которой запущен скрипт |",
        "| Данные | учебные программы и открытые наборы PSPLIB и RCPSP/max |",
        "| Дата | |",
        "| Участники | |",
        "",
        f"Лабораторное подтверждение: {CLAIM_LEVEL}, ISO 16290 TRL {ISO16290_TRL}.",
        "",
        "## Автоматическая часть",
        "",
        "| Проверка | Результат |",
        "|---|---|",
        f"| `pytest` | {pytest_line} |",
        "",
        "Цифры взяты из JSON в `benchmarks/`.",
        "",
        "| Файл | Экземпляры | Принято | OPTIMAL | Неотвечено | Ложные утверждения | Среднее время, с |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in BENCH:
        lines.append(_bench_row(ROOT / "benchmarks" / name))
    lines.extend(
        [
            "",
            "## Стенд (раздел 3 методики)",
            "",
            "На площадке заказчика в эти пункты ставятся числа из вывода команд.",
            "",
            "1. Импорт обезличенного файла: число работ, связей и ресурсов совпало с источником.",
            "2. Эксперт отметил, какие конфликты подтверждаются.",
            "3. Четыре варианта: время до первого допустимого плана, до улучшенного и до набора.",
            "4. Одна правка: сдвиги по связям, на общих ресурсах и без связи с правкой.",
            "5. Журнал: принятие и отклонение, `journal` и сверка `--anchor`.",
            "6. Сеть: локальный адрес за прокси с TLS; внешний адрес без сертификата отказал.",
            "",
            "## Решение",
            "",
            "Подписи заказчика и исполнителя, дата.",
            "",
        ]
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    return 0


def _bench_row(path: Path) -> str:
    if not path.is_file():
        return f"| `{path.name}` | — | — | — | — | — | — |"
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary")
    if isinstance(summary, dict) and "instances" in summary:
        return (
            f"| `{path.name}` | {summary.get('instances', '—')} | {summary.get('accepted', '—')} | "
            f"{summary.get('optimal_claims', '—')} | {summary.get('unanswered', '—')} | "
            f"{summary.get('false_claims', '—')} | {summary.get('mean_seconds', '—')} |"
        )
    rows = payload.get("rows") or []
    accepted = sum(1 for row in rows if row.get("ok"))
    return f"| `{path.name}` | {len(rows)} | {accepted} | — | — | — | — |"


if __name__ == "__main__":
    raise SystemExit(main())
