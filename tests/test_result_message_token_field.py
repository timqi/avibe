from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im.formatters.slack_formatter import SlackFormatter


def test_result_footer_uses_status_emoji_time_and_token_glyphs() -> None:
    formatter = SlackFormatter()

    footer = formatter.format_result_footer(
        "success",
        duration_ms=144_000,
        token_field="240k tok",
    )

    assert footer == "✅ ⏱️ 2m 24s · 🪙 240k tok"


def test_result_footer_omits_token_field_when_empty() -> None:
    formatter = SlackFormatter()

    footer = formatter.format_result_footer("success", duration_ms=5_000, token_field="")

    assert footer == "✅ ⏱️ 5s"


def test_result_footer_shows_token_without_duration() -> None:
    formatter = SlackFormatter()

    footer = formatter.format_result_footer("success", duration_ms=0, token_field="12.3k tok")

    assert footer == "✅ 🪙 12.3k tok"


def test_result_footer_bare_marker_when_nothing_else() -> None:
    formatter = SlackFormatter()

    footer = formatter.format_result_footer(
        "success", duration_ms=0, token_field="", show_duration=False
    )

    assert footer == "✅"


def test_result_footer_error_and_warning_markers() -> None:
    formatter = SlackFormatter()

    error = formatter.format_result_footer("error_max_turns", duration_ms=5_000)
    warning = formatter.format_result_footer("warning", duration_ms=5_000)

    assert error == "❌ ⏱️ 5s"
    assert warning == "⚠️ ⏱️ 5s"


def test_result_message_keeps_footer_above_body_for_legacy_path() -> None:
    formatter = SlackFormatter()

    rendered = formatter.format_result_message(
        "success",
        duration_ms=144_000,
        result="all done",
        show_duration=True,
        token_field="240k tok",
    )

    assert rendered == "✅ ⏱️ 2m 24s · 🪙 240k tok\n\nall done"
