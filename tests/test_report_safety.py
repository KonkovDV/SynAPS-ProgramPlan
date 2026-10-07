from __future__ import annotations

import base64
import hashlib
import re
from importlib import resources

from synaps_programplan.report import render_html

_PAGE = {
    "program": {"id": "p", "name": "Учебная"},
    "projects": [],
    "resources": [],
    "scenarios": [],
}


def _script_src(policy: str) -> str:
    for part in policy.split(";"):
        item = part.strip()
        if item.startswith("script-src"):
            return item
    raise AssertionError(policy)


_ATTACKS = (
    "</script><script>alert(1)</script>",
    "</ScRiPt><script>alert(1)</script>",
    "<&",
    "line\u2028sep",
    "para\u2029sep",
)


def test_imported_names_cannot_close_the_report_script() -> None:
    for text in _ATTACKS:
        page = render_html(
            {
                "program": {"id": text, "name": text},
                "projects": [{"id": text, "code": text, "name": text}],
                "resources": [{"id": text, "name": text, "code": text}],
                "scenarios": [{"tasks": [{"name": text, "why": {"text": text}}]}],
            },
            title=text,
        )
        marker = '<script type="application/json" id="data">'
        blob = page[page.index(marker) + len(marker) : page.lower().index("</script>", page.index(marker))]
        assert "<" not in blob
        assert ">" not in blob
        assert "&" not in blob
        assert "\u2028" not in blob
        assert "\u2029" not in blob


def test_report_script_is_pinned_by_its_hash() -> None:
    page = render_html(_PAGE)
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', page)
    assert policy is not None
    script_src = _script_src(policy.group(1))
    assert "unsafe-inline" not in script_src
    bodies = re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.DOTALL)
    assert len(bodies) == 2
    for body in bodies:
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        assert f"'sha256-{digest}'" in script_src
    template = resources.files("synaps_programplan").joinpath("report_template.html").read_text("utf-8")
    assert "script-src 'unsafe-inline'" not in template
    assert "style-src 'unsafe-inline'" in policy.group(1)
