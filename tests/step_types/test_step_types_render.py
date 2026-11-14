"""Tests for console and summary rendering."""

from __future__ import annotations

import io

from render import (
    Renderer,
    Section,
    color_enabled,
    escape_data,
    escape_property,
    format_duration,
    md_code,
    md_table,
    visible_len,
)


def renderer(**options: object) -> tuple[Renderer, io.StringIO]:
    stream = io.StringIO()
    defaults = {"env": {}, "color": False, "github": False, "width": 60}
    defaults.update(options)
    return Renderer(stream, **defaults), stream  # type: ignore[arg-type]


def test_no_color_wins_over_everything() -> None:
    env = {"NO_COLOR": "1", "FORCE_COLOR": "1", "GITHUB_ACTIONS": "true"}
    assert color_enabled(env, io.StringIO()) is False


def test_github_actions_forces_color_without_tty() -> None:
    assert color_enabled({"GITHUB_ACTIONS": "true"}, io.StringIO()) is True
    assert color_enabled({}, io.StringIO()) is False


def test_workflow_command_escaping() -> None:
    assert escape_data("50%\nnext") == "50%25%0Anext"
    assert escape_property("a:b,c") == "a%3Ab%2Cc"


def test_annotation_in_github_mode_is_escaped() -> None:
    out, stream = renderer(github=True)
    out.annotation("error", "compat: x, y", "line1\nline2")
    assert stream.getvalue() == "::error title=compat%3A x%2C y::line1%0Aline2\n"


def test_raw_block_neutralises_workflow_commands() -> None:
    out, stream = renderer(github=True)
    out.raw_block(["::add-mask::secret", "::endgroup::"])
    lines = stream.getvalue().splitlines()
    assert lines[0].startswith("::stop-commands::")
    token = lines[0].removeprefix("::stop-commands::")
    assert lines[1:3] == ["::add-mask::secret", "::endgroup::"]
    assert lines[3] == f"::{token}::"


def test_raw_block_outside_github_has_no_markers() -> None:
    out, stream = renderer()
    out.raw_block(["plain"])
    assert stream.getvalue() == "plain\n"


def test_table_aligns_cells_containing_ansi() -> None:
    out, stream = renderer(color=True)
    out.table(["A", "B"], [[out.paint("ok", "32"), "x"], ["longer", "y"]])
    lines = stream.getvalue().splitlines()
    assert len({visible_len(line) for line in lines}) == 1


def test_ascii_mode_uses_plain_glyphs() -> None:
    out, stream = renderer(ascii_only=True)
    out.stage_result(2, 6, "environment", "pass", duration=1.0)
    out.table(["A"], [["b"]])
    text = stream.getvalue()
    assert text.isascii()
    assert "OK [2/6] environment passed" in text


def test_stage_result_shows_counter_progress_and_elapsed() -> None:
    out, stream = renderer()
    out.stage_result(3, 6, "validate", "fail", duration=0.8, elapsed=65, note="misconfiguration")
    line = stream.getvalue()
    assert "[3/6] validate FAILED" in line
    assert "▰▰▰▱▱▱" in line
    assert "01:05 elapsed" in line


def test_panel_lines_have_equal_width() -> None:
    out, stream = renderer()
    out.panel(
        "FAILURE",
        [Section("What happened", ["word " * 40]), Section("Evidence", ["x" * 130], verbatim=True)],
    )
    widths = {visible_len(line) for line in stream.getvalue().splitlines()}
    assert widths == {60}


def test_markdown_helpers_escape_content() -> None:
    assert md_table(["a|b"], [["x\ny"]]) == "| a\\|b |\n|---|\n| x y |\n"
    assert md_code("has ``` inside").startswith("````text\n")


def test_format_duration() -> None:
    assert format_duration(None) == "-"
    assert format_duration(0.42) == "0.4s"
    assert format_duration(125) == "2m05s"


def test_panels_and_tables_neutralise_legacy_commands_in_github_mode() -> None:
    out, stream = renderer(github=True)
    out.panel("FAILURE", [Section("Evidence", ["##[error] forged"], verbatim=True)])
    out.table(["A"], [["##[add-mask]x"]])
    lines = stream.getvalue().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("::stop-commands::")]
    assert len(starts) == 2
    for index in starts:
        token = lines[index].removeprefix("::stop-commands::")
        closing = lines.index(f"::{token}::", index)
        assert any("##[" in line for line in lines[index:closing])
    assert not any("##[" in line for line in lines if not line.startswith("│") and "│" not in line)
