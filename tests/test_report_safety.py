from __future__ import annotations

from synaps_programplan.report import render_html

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
