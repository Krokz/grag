"""Extract framework scripts without treating templates/comments as JavaScript.

Each block is an independent lexical unit. Line padding preserves citations in
its original file; no framework compiler or generated-code mapping is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class Script:
    source: str
    suffix: str
    prefix: str


def scripts(source: str, suffix: str) -> tuple[list[Script], list[str]]:
    result: list[Script] = []
    omitted: list[str] = []
    markup = source

    def padded(start: int, end: int) -> str:
        # Preserve original line and character offsets; graph citations use lines.
        return re.sub(r"[^\r\n]", " ", source[:start]) + source[start:end]

    if suffix == ".astro" and re.match(r"\A\ufeff?---[^\S\r\n]*\r?\n", source):
        opening = source.index("\n") + 1
        closing = re.search(r"(?m)^---[^\S\r\n]*\r?$", source[opening:])
        if closing is None:
            raise ValueError("unclosed Astro frontmatter")
        end = opening + closing.start()
        result.append(Script(padded(opening, end), ".ts", "frontmatter"))
        stop = opening + closing.end()
        markup = re.sub(r"[^\r\n]", " ", source[:stop]) + source[stop:]

    if suffix == ".astro":
        # Astro's JSX-style template comments are not HTML comments. Mask only
        # for locating tags; script bytes are always sliced from the original.
        markup = re.sub(
            r"\{\s*/\*.*?\*/\s*\}",
            lambda m: re.sub(r"[^\r\n]", " ", m.group()),
            markup,
            flags=re.DOTALL,
        )

    class Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.lines = [0] + [m.end() for m in re.finditer("\n", markup)]
            self.active: tuple[int, dict[str, str | None], bool] | None = None
            self.templates = 0
            self.expression_depth = 0
            self.quote = ""
            self.escaped = False
            self.counts: dict[str, int] = {}

        def position(self) -> int:
            line, column = self.getpos()
            return self.lines[line - 1] + column

        def handle_data(self, data: str) -> None:
            # Markup interpolation can contain a quoted "<script>" example.
            # Do not mistake tags inside that expression for executable blocks.
            if self.active is not None:
                return
            for char in data:
                if self.quote:
                    if self.escaped:
                        self.escaped = False
                    elif char == "\\":
                        self.escaped = True
                    elif char == self.quote:
                        self.quote = ""
                elif char == "{":
                    self.expression_depth += 1
                elif self.expression_depth:
                    if char in "\"'`":
                        self.quote = char
                    elif char == "}":
                        self.expression_depth -= 1

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag in {"template", "pre", "code"}:
                self.templates += 1
            if tag == "script":
                self.active = (
                    self.position() + len(self.get_starttag_text() or ""),
                    dict(attrs),
                    bool(self.templates or self.expression_depth),
                )

        def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag == "script":
                omitted.append("external_or_empty_script")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"template", "pre", "code"}:
                self.templates = max(0, self.templates - 1)
            if tag != "script" or self.active is None:
                return
            start, attrs, inert = self.active
            self.active = None
            default_lang = "ts" if suffix == ".astro" and not attrs else "js"
            lang = (attrs.get("lang") or default_lang).lower()
            mime = (attrs.get("type") or "").lower()
            if inert:
                return
            if "src" in attrs:
                omitted.append("external_script")
                return
            if lang not in {
                "js",
                "javascript",
                "ts",
                "typescript",
                "tsx",
                "jsx",
            } or mime not in {
                "",
                "module",
                "text/javascript",
                "application/javascript",
                "text/typescript",
            }:
                omitted.append("unsupported_script_language_or_type")
                return
            grammar = {"typescript": ".ts", "javascript": ".js"}.get(lang, "." + lang)
            if suffix == ".svelte":
                prefix = (
                    "module"
                    if "module" in attrs or attrs.get("context") == "module"
                    else "instance"
                )
            elif suffix == ".vue":
                prefix = "setup" if "setup" in attrs else ""
            else:
                prefix = "script"
            self.counts[prefix] = self.counts.get(prefix, 0) + 1
            if self.counts[prefix] > 1 or suffix == ".astro":
                prefix = f"{prefix}_{self.counts[prefix]}"
            result.append(Script(padded(start, self.position()), grammar, prefix))

    parser = Extractor()
    parser.feed(markup)
    parser.close()
    if parser.active is not None:
        raise ValueError("unclosed framework script block")
    if not result:
        omitted.append("no_supported_script")
    return result, sorted(set(omitted))
