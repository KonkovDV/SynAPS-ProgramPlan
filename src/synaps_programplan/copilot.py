"""Read-only language-model note over facts of an accepted plan.

The model is off until the operator sets a Yandex Cloud API key and folder.
It receives facts already computed by the planner and the checker. It does not
create constraints, move tasks, or publish a plan. A sentence that does not
cite a known fact is dropped.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from synaps_programplan.model import OKRProgram
from synaps_programplan.result import PlanResult

YANDEX_CHAT_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"
_MAX_FACTS = 200

_SYSTEM = (
    "Ты готовишь записку планировщику только по переданным фактам принятого плана. "
    "Ответь одним JSON-объектом вида "
    '{"statements":[{"text":"...","fact_ids":["task:ID"]}]}. '
    "Каждая фраза обязана ссылаться на идентификаторы фактов из списка. "
    "Не добавляй работы, сроки, ресурсы и ограничения, которых нет в фактах. "
    "Не предлагай изменить план и не называй срок невыполнимым."
)


@dataclass(frozen=True)
class Fact:
    id: str
    text: str


@dataclass(frozen=True)
class Statement:
    text: str
    fact_ids: list[str]


@dataclass(frozen=True)
class Answer:
    statements: list[Statement]
    dropped: int
    model: str


def yandex_settings() -> tuple[str, str, str] | None:
    """API key, folder and model name, or None when the model stays off."""
    key = os.environ.get("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", "").strip()
    folder = os.environ.get("SYNAPS_PROGRAMPLAN_YANDEX_FOLDER", "").strip()
    if not key or not folder:
        return None
    model = os.environ.get("SYNAPS_PROGRAMPLAN_YANDEX_MODEL", "yandexgpt").strip() or "yandexgpt"
    return key, folder, model


def facts_of(program: OKRProgram, result: PlanResult) -> list[Fact]:
    """Facts a note may cite. An unaccepted plan is refused before any request."""
    if not result.outcome.ok:
        raise ValueError("the language model reads only an accepted plan")
    articles = {row.id: row for row in program.articles}
    facts: list[Fact] = []
    for article in program.articles:
        facts.append(
            Fact(
                f"article:{article.id}",
                f"Опытный экземпляр {article.code} ({article.name}) занимает ресурс {article.resource_id}.",
            )
        )
    for explained in result.explanations:
        facts.append(Fact(f"explanation:{explained.task_id}", explained.text))
    shifted = [item for item in result.tasks if item.shift_wd]
    rest = [item for item in result.tasks if not item.shift_wd]
    for planned in shifted + rest:
        article_id = program.task(planned.task_id).test_article_id
        linked = articles.get(article_id) if article_id else None
        piece = (
            f"«{planned.name}» ({planned.task_id}) "
            f"с {planned.start.isoformat()} по {planned.finish.isoformat()}"
        )
        if planned.shift_wd:
            piece += f", сдвиг {planned.shift_wd} раб. дн."
        if linked is not None:
            piece += f", экземпляр {linked.code}"
        facts.append(Fact(f"task:{planned.task_id}", piece))
        if len(facts) >= _MAX_FACTS:
            break
    return facts[:_MAX_FACTS]


def yandex_body(folder: str, model: str, messages: list[dict[str, str]]) -> dict[str, object]:
    """OpenAI-compatible body. Official base is ``https://ai.api.cloud.yandex.net/v1``."""
    return {
        "model": f"gpt://{folder}/{model}/latest",
        "temperature": 0,
        "messages": messages,
    }


def yandex_complete(key: str, folder: str, model: str, messages: list[dict[str, str]]) -> str:
    """One chat completion. The key is sent as ``Authorization: Api-Key`` and is not logged."""
    payload = json.dumps(yandex_body(folder, model, messages), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        YANDEX_CHAT_URL,
        data=payload,
        headers={
            "Authorization": f"Api-Key {key}",
            "OpenAI-Project": folder,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(f"Yandex Cloud answered {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Yandex Cloud is unreachable: {exc.reason}") from exc
    body = json.loads(raw)
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Yandex Cloud returned no message") from exc


def ask(question: str, facts: list[Fact], complete: Callable[[list[dict[str, str]]], str]) -> Answer:
    """Keep only sentences whose fact ids are in ``facts``. No facts, no request."""
    if not facts:
        raise ValueError("the language model is refused without plan facts")
    known = {fact.id: fact.text for fact in facts}
    listing = "\n".join(f"{fact.id}: {fact.text}" for fact in facts)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Факты:\n{listing}\n\nВопрос: {question}"},
    ]
    parsed = _statements(complete(messages))
    kept: list[Statement] = []
    dropped = 0
    for text, cited in parsed:
        ids = [item for item in cited if item in known]
        if not text.strip() or not ids or len(ids) != len(cited):
            dropped += 1
            continue
        kept.append(Statement(text.strip(), ids))
    return Answer(kept, dropped, "yandex")


def _statements(raw: str) -> list[tuple[str, list[str]]]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    rows = payload.get("statements") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, list[str]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cited = row.get("fact_ids")
        if not isinstance(cited, list) or not all(isinstance(item, str) for item in cited):
            out.append((str(row.get("text") or ""), []))
            continue
        out.append((str(row.get("text") or ""), list(cited)))
    return out
