from __future__ import annotations

import pytest

from synaps_programplan.copilot import Fact, ask, facts_of, yandex_body, yandex_settings
from synaps_programplan.result import Claim, Outcome, PlanResult
from tests.conftest import program, task


def _rejected() -> PlanResult:
    return PlanResult(
        scenario_id="A",
        label="A",
        tasks=[],
        violations=[],
        outcome=Outcome(
            ok=False,
            claim=Claim.INFEASIBLE,
            solver_status="infeasible",
            solver_config="cpsat",
            kernel_verified=True,
            domain_hard_violations=0,
        ),
    )


def test_an_unaccepted_plan_is_not_read() -> None:
    with pytest.raises(ValueError, match="accepted plan"):
        facts_of(program([task("a", 1)]), _rejected())


def test_ask_without_facts_is_refused() -> None:
    with pytest.raises(ValueError, match="without plan facts"):
        ask("почему", [], lambda _messages: '{"statements":[]}')


def test_a_statement_without_a_known_fact_is_dropped() -> None:
    facts = [Fact("task:a", "работа a с 2026-10-05 по 2026-10-07")]

    def complete(_messages: list[dict[str, str]]) -> str:
        return (
            '{"statements":['
            '{"text":"сдвиг из-за неизвестного стенда","fact_ids":["task:missing"]},'
            '{"text":"работа a стоит в начале октября","fact_ids":["task:a"]}'
            "]}"
        )

    answer = ask("что с работой a", facts, complete)
    assert [row.text for row in answer.statements] == ["работа a стоит в начале октября"]
    assert answer.dropped == 1


def test_prose_without_facts_is_dropped() -> None:
    answer = ask("почему", [Fact("task:a", "работа a")], lambda _messages: "Я так думаю, стенд свободен.")
    assert answer.statements == []
    assert answer.dropped == 0


def test_yandex_is_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_YANDEX_FOLDER", raising=False)
    assert yandex_settings() is None


def test_yandex_body_uses_the_folder_model() -> None:
    body = yandex_body("b1gfolder", "yandexgpt", [{"role": "user", "content": "вопрос"}])
    assert body["model"] == "gpt://b1gfolder/yandexgpt/latest"
    assert body["temperature"] == 0


def test_a_figure_the_cited_facts_do_not_contain_is_dropped() -> None:
    facts = [Fact("task:a", "работа a с 2026-10-05 по 2026-10-07, сдвиг 2 раб. дн.")]

    def complete(_messages: list[dict[str, str]]) -> str:
        return (
            '{"statements":['
            '{"text":"работа a завершится 12.10.2026","fact_ids":["task:a"]},'
            '{"text":"работа a начнётся 05.10.2026 и сдвинута на 2 раб. дн.","fact_ids":["task:a"]}'
            "]}"
        )

    answer = ask("когда работа a", facts, complete)
    assert [row.text for row in answer.statements] == [
        "работа a начнётся 05.10.2026 и сдвинута на 2 раб. дн."
    ]
    assert answer.dropped == 1
