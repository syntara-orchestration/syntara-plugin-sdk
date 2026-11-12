"""Console and step-summary rendering for the step types compatibility check.

Everything the check prints goes through this module so the log reads like a
report: banners, numbered stage progress, box-drawn tables, a failure panel,
GitHub workflow commands (groups and annotations), and markdown for the job
summary. Standard library only.

Colour policy: ``NO_COLOR`` always wins; otherwise colour is on when
``FORCE_COLOR`` is set, when running in GitHub Actions, or when the stream is a
TTY. ``STEP_TYPES_ASCII=1`` replaces every glyph with plain ASCII.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

RESET = "0"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"

_UNICODE = {
    "pass": "✔",
    "fail": "✘",
    # ASCII on purpose: circled glyphs render double-width in common log
    # fonts (including the GitHub Actions log), pushing the skip rows of
    # every table past their border.
    "skip": "-",
    "warn": "!",
    "start": "▶",
    "bar_on": "▰",
    "bar_off": "▱",
    "dot": "·",
    "tl": "┌",
    "tr": "┐",
    "bl": "└",
    "br": "┘",
    "h": "─",
    "v": "│",
    "lt": "├",
    "rt": "┤",
    "tt": "┬",
    "bt": "┴",
    "x": "┼",
    "dtl": "╔",
    "dtr": "╗",
    "dbl": "╚",
    "dbr": "╝",
    "dh": "═",
    "dv": "║",
    "rtl": "╭",
    "rtr": "╮",
    "rbl": "╰",
    "rbr": "╯",
}

_ASCII = {
    "pass": "OK",
    "fail": "FAIL",
    "skip": "SKIP",
    "warn": "WARN",
    "start": ">",
    "bar_on": "#",
    "bar_off": "-",
    "dot": "-",
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "h": "-",
    "v": "|",
    "lt": "+",
    "rt": "+",
    "tt": "+",
    "bt": "+",
    "x": "+",
    "dtl": "+",
    "dtr": "+",
    "dbl": "+",
    "dbr": "+",
    "dh": "=",
    "dv": "|",
    "rtl": "+",
    "rtr": "+",
    "rbl": "+",
    "rbr": "+",
}

STATUS_COLOR = {"pass": GREEN, "fail": RED, "skip": YELLOW, "warn": YELLOW}
PANEL_WIDTH = 96


def visible_len(text: str) -> int:
    """Return the printed width of ``text`` with ANSI escapes removed."""
    return len(_ANSI.sub("", text))


def color_enabled(env: Mapping[str, str], stream: TextIO) -> bool:
    """Decide whether ANSI colour is used, honouring the NO_COLOR convention."""
    if env.get("NO_COLOR"):
        return False
    if env.get("FORCE_COLOR") or env.get("GITHUB_ACTIONS") == "true":
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def escape_data(value: str) -> str:
    """Escape a workflow-command message (the part after ``::``)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """Escape a workflow-command property value such as ``title=``."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def format_duration(seconds: float | None) -> str:
    """Render a duration compactly: ``0.4s``, ``12.3s``, ``2m05s``."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m{rest:02d}s"


def format_clock(seconds: float) -> str:
    """Render elapsed time as ``MM:SS``."""
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes:02d}:{rest:02d}"


@dataclass(frozen=True)
class Section:
    """One labelled block inside a panel."""

    title: str
    lines: Sequence[str]
    verbatim: bool = False


class Renderer:
    """Write structured, optionally coloured output to one stream."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        env: Mapping[str, str] | None = None,
        color: bool | None = None,
        ascii_only: bool | None = None,
        github: bool | None = None,
        width: int | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        environment = os.environ if env is None else env
        self.color = color_enabled(environment, self.stream) if color is None else color
        if ascii_only is None:
            ascii_only = environment.get("STEP_TYPES_ASCII") == "1"
        self.glyphs = _ASCII if ascii_only else _UNICODE
        self.dot = self.glyphs["dot"]
        self.github = environment.get("GITHUB_ACTIONS") == "true" if github is None else github
        if width is None:
            width = min(PANEL_WIDTH, max(60, shutil.get_terminal_size((PANEL_WIDTH, 24)).columns))
        self.width = width

    # Basic output -----------------------------------------------------------

    def paint(self, text: str, *codes: str) -> str:
        """Wrap ``text`` in ANSI codes when colour is enabled."""
        if not self.color or not codes or not text:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def blank(self) -> None:
        self.line()

    def rule(self, label: str = "", code: str = DIM) -> None:
        """Print a horizontal divider, optionally with an inline label."""
        h = self.glyphs["h"]
        if not label:
            self.line(self.paint(h * self.width, code))
            return
        prefix = f"{h * 3} {label} "
        self.line(self.paint(prefix + h * max(3, self.width - visible_len(prefix)), code))

    # Banner -----------------------------------------------------------------

    def banner(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Print a double-ruled box with a title and aligned key/value rows."""
        g = self.glyphs
        inner = self.width - 2
        key_width = max((len(key) for key, _ in rows), default=0)
        self.line(self.paint(g["dtl"] + g["dh"] * inner + g["dtr"], CYAN))
        self._banner_row(self.paint(title, BOLD, CYAN), inner)
        self.line(self.paint(g["dv"], CYAN) + " " * inner + self.paint(g["dv"], CYAN))
        for key, value in rows:
            available = inner - key_width - 4
            wrapped = textwrap.wrap(value, available) or [""]
            for index, part in enumerate(wrapped):
                label = key.ljust(key_width) if index == 0 else " " * key_width
                self._banner_row(f"{self.paint(label, DIM)}  {part}", inner)
        self.line(self.paint(g["dbl"] + g["dh"] * inner + g["dbr"], CYAN))

    def _banner_row(self, content: str, inner: int) -> None:
        g = self.glyphs
        padding = max(0, inner - 2 - visible_len(content))
        self.line(
            self.paint(g["dv"], CYAN) + "  " + content + " " * padding + self.paint(g["dv"], CYAN)
        )

    # Stage progress ---------------------------------------------------------

    def progress_bar(self, done: int, total: int) -> str:
        g = self.glyphs
        return g["bar_on"] * done + g["bar_off"] * max(0, total - done)

    def stage_start(self, index: int, total: int, name: str, detail: str = "") -> None:
        counter = f"[{index}/{total}]"
        text = f"{self.glyphs['start']} {self.paint(counter, BOLD)} {self.paint(name, BOLD, CYAN)}"
        if detail:
            text += f" {self.paint(self.glyphs['dot'] + ' ' + detail, DIM)}"
        self.line(text)

    def stage_result(
        self,
        index: int,
        total: int,
        name: str,
        status: str,
        *,
        duration: float | None = None,
        elapsed: float | None = None,
        note: str = "",
    ) -> None:
        glyph = self.glyphs[status]
        code = STATUS_COLOR.get(status, RESET)
        words = {"pass": "passed", "fail": "FAILED", "skip": "skipped", "warn": "warning"}
        parts = [
            self.paint(f"{glyph} [{index}/{total}] {name} {words.get(status, status)}", BOLD, code)
        ]
        if duration is not None:
            parts.append(self.paint(f"({format_duration(duration)})", DIM))
        if note:
            parts.append(self.paint(note, code if status == "fail" else DIM))
        tail = self.progress_bar(index, total)
        if elapsed is not None:
            tail += f" {format_clock(elapsed)} elapsed"
        parts.append(self.paint(tail, DIM))
        self.line(" ".join(parts))

    # GitHub workflow commands -----------------------------------------------

    def group_start(self, title: str) -> None:
        if self.github:
            self.line(f"::group::{escape_data(title)}")
        else:
            self.line(self.paint(f"{self.glyphs['h'] * 2} {title}", DIM))

    def group_end(self) -> None:
        if self.github:
            self.line("::endgroup::")

    def begin_raw(self) -> str:
        """Start a region of untrusted text; returns the token for :meth:`end_raw`.

        Output from commands under test could contain ``::`` workflow commands
        (for example ``::add-mask::`` or ``::error``); between
        ``::stop-commands::<token>`` and ``::<token>::`` GitHub prints them as
        plain text instead of executing them.
        """
        token = secrets.token_hex(16) if self.github else ""
        if token:
            self.line(f"::stop-commands::{token}")
        return token

    def raw_line(self, text: str, label: str = "") -> None:
        prefix = self.paint(f"{label} ", DIM) if label else ""
        self.line(prefix + text.rstrip("\r\n"))

    def end_raw(self, token: str) -> None:
        if token:
            self.line(f"::{token}::")

    def raw_block(self, lines: Iterable[str], *, label: str = "") -> None:
        """Print untrusted text, neutralising any workflow commands inside it."""
        token = self.begin_raw()
        for text in lines:
            self.raw_line(text, label)
        self.end_raw(token)

    def annotation(self, level: str, title: str, message: str) -> None:
        """Emit an error/warning/notice annotation (plain text outside Actions)."""
        if self.github:
            self.line(f"::{level} title={escape_property(title)}::{escape_data(message)}")
            return
        code = RED if level == "error" else YELLOW
        self.line(self.paint(f"{level.upper()}: {title}: {message}", code))

    # Tables -----------------------------------------------------------------

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        aligns: Sequence[str] | None = None,
        row_colors: Sequence[str | None] | None = None,
    ) -> None:
        """Print a box-drawn table; cell text may already contain ANSI codes."""
        g = self.glyphs
        aligns = list(aligns or ["l"] * len(headers))
        widths = [visible_len(h) for h in headers]
        for row in rows:
            for column, cell in enumerate(row):
                widths[column] = max(widths[column], visible_len(cell))

        def border(left: str, middle: str, right: str) -> str:
            return self.paint(left + middle.join(g["h"] * (w + 2) for w in widths) + right, DIM)

        def render_row(cells: Sequence[str], code: str | None = None) -> str:
            rendered = []
            for column, cell in enumerate(cells):
                pad = widths[column] - visible_len(cell)
                text = " " * pad + cell if aligns[column] == "r" else cell + " " * pad
                if code:
                    text = self.paint(text, code)
                rendered.append(f" {text} ")
            separator = self.paint(g["v"], DIM)
            return separator + separator.join(rendered) + separator

        # Cells can carry text from the commands under test; see begin_raw.
        token = self.begin_raw()
        self.line(border(g["tl"], g["tt"], g["tr"]))
        self.line(render_row([self.paint(h, BOLD) for h in headers]))
        self.line(border(g["lt"], g["x"], g["rt"]))
        for index, row in enumerate(rows):
            code = row_colors[index] if row_colors else None
            self.line(render_row(row, code))
        self.line(border(g["bl"], g["bt"], g["br"]))
        self.end_raw(token)

    # Panels -----------------------------------------------------------------

    def panel(self, title: str, sections: Sequence[Section], *, tone: str = RED) -> None:
        """Print a rounded box with titled sections, wrapped to the panel width.

        Panels quote evidence verbatim from the commands under test, so the whole
        panel is a stop-commands region: the runner's legacy ``##[command]`` syntax
        is recognised anywhere in a line, not only at its start.
        """
        g = self.glyphs
        inner = self.width - 2
        heading = f"{g['h']} {title} "
        token = self.begin_raw()
        self.line(
            self.paint(g["rtl"], tone)
            + self.paint(heading, BOLD, tone)
            + self.paint(g["h"] * max(0, inner - len(heading)) + g["rtr"], tone)
        )
        for position, section in enumerate(sections):
            if position:
                self._panel_row("", inner, tone)
            self._panel_row(self.paint(section.title, BOLD), inner, tone)
            for text in section.lines:
                for part in self._wrap(text, inner - 6, verbatim=section.verbatim):
                    self._panel_row("  " + part, inner, tone)
        self.line(self.paint(g["rbl"] + g["h"] * inner + g["rbr"], tone))
        self.end_raw(token)

    def _wrap(self, text: str, width: int, *, verbatim: bool) -> list[str]:
        if verbatim:
            return [text[i : i + width] for i in range(0, max(len(text), 1), width)] or [""]
        return (
            textwrap.wrap(text, width, subsequent_indent="  ", break_on_hyphens=False) or [""]
        )

    def _panel_row(self, content: str, inner: int, tone: str) -> None:
        bar = self.paint(self.glyphs["v"], tone)
        padding = max(0, inner - 2 - visible_len(content))
        self.line(f"{bar}  {content}{' ' * padding}{bar}")


# Markdown (job summary) -------------------------------------------------------


def md_escape(value: str) -> str:
    """Make ``value`` safe inside a markdown table cell."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(md_escape(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def md_code(text: str) -> str:
    """Fence ``text`` so no backtick run inside it can close the block early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}\n"


def append_summary(markdown: str, env: Mapping[str, str] | None = None) -> None:
    """Append markdown to the GitHub job summary when one is available."""
    path = (os.environ if env is None else env).get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown)
        if not markdown.endswith("\n"):
            handle.write("\n")
