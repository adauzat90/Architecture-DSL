"""A canonical, comment-preserving formatter for ``.barn`` source.

``emit_dsl`` regenerates source from the compiled *model* — a perfect round-trip
of the geometry, but it DROPS every comment (teaching notes, the gallery's prose,
and suppression pragmas). A formatter that deletes comments is unusable, so ``fmt``
is a **line-preserving normalizer** instead: it re-renders each statement line from
its OWN tokens, keeps comment lines and trailing comments verbatim, and leaves
statement order and blank lines exactly where they are.

What it normalizes (per statement line):

* run-length whitespace → a single space (drops column alignment),
* numbers → the ``:g`` canonical form emit uses, and feet-and-inches literals
  (``12-6``, ``12'6``, ``12′6″``) → decimal feet,
* the leading statement keyword → lower-case.

Punctuation (``:`` after an id, ``,`` in a coordinate) is preserved and glued the
way ``emit_dsl`` writes it. A line that doesn't tokenize is passed through
unchanged — ``fmt`` never destroys content.

The contract is **idempotence**: ``format_source(format_source(x)) ==
format_source(x)``.
"""

from __future__ import annotations

from .compiler import _KEYWORDS, _parse_ft_in

_KEYWORD_SET = frozenset(_KEYWORDS)
#: Braces are optional syntactic sugar the lexer drops; the canonical form omits
#: them, so the formatter skips them too (matching ``emit_dsl``).
_DROP = frozenset("{}")


def _comment_start(line: str) -> int | None:
    """Index of the first ``#`` that begins a comment (not one inside a string),
    or ``None``. Mirrors the lexer's quote handling."""
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            return i
        if ch == '"':
            i += 1
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        i += 1
    return None


def _norm_word(text: str, is_first: bool) -> str:
    """Normalize one bare word: lower-case the leading statement keyword, render a
    number in ``:g``, canonicalize a feet-and-inches literal to decimal feet, and
    otherwise leave it untouched (identifiers keep their case)."""
    if is_first and text.lower() in _KEYWORD_SET:
        return text.lower()
    try:
        return f"{float(text):g}"
    except ValueError:
        pass
    ft_in = _parse_ft_in(text)
    if ft_in is not None:
        return f"{ft_in:g}"
    return text


def _ftokens(stmt: str) -> list[tuple[str, str]] | None:
    """Tokenize a statement portion for formatting, preserving ``:`` and ``,`` as
    structural tokens and quoted strings verbatim. Returns ``None`` if a string
    literal is unterminated (the caller then passes the line through untouched)."""
    toks: list[tuple[str, str]] = []
    i, n = 0, len(stmt)
    while i < n:
        ch = stmt[i]
        if ch in " \t" or ch in _DROP:
            i += 1
            continue
        if ch == '"':
            start = i
            i += 1
            terminated = False
            while i < n:
                if stmt[i] == '"':
                    i += 1
                    terminated = True
                    break
                if stmt[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            if not terminated:
                return None
            toks.append(("str", stmt[start:i]))
            continue
        if ch == ":":
            toks.append(("colon", ":"))
            i += 1
            continue
        if ch == ",":
            toks.append(("comma", ","))
            i += 1
            continue
        start = i
        while i < n and stmt[i] not in ' \t":,' and stmt[i] not in _DROP:
            i += 1
        toks.append(("word", stmt[start:i]))
    return toks


def _render(toks: list[tuple[str, str]]) -> str:
    """Re-join format-tokens with canonical spacing (single spaces; ``:`` glued to
    its id then a space; ``,`` glued both sides)."""
    parts: list[str] = []
    glue_next = True  # the first token takes no leading space
    first_word = True

    def put(txt: str, glue: bool) -> None:
        if parts and not glue:
            parts.append(" ")
        parts.append(txt)

    for t, txt in toks:
        if t == "colon":
            put(":", True)
            glue_next = False
        elif t == "comma":
            put(",", True)
            glue_next = True
        else:
            rendered = _norm_word(txt, first_word) if t == "word" else txt
            put(rendered, glue_next)
            glue_next = False
            first_word = False
    return "".join(parts)


def format_line(line: str) -> str:
    """Format a single source line (see the module docstring)."""
    cut = _comment_start(line)
    stmt = line if cut is None else line[:cut]
    comment = None if cut is None else line[cut:]
    if not stmt.strip():
        # Blank line, or a comment-only line: normalize a blank to empty, keep the
        # comment verbatim (with its leading whitespace stripped, so it's stable).
        return comment if comment is not None else ""
    toks = _ftokens(stmt)
    if not toks:  # unterminated string, or nothing tokenizable — pass through
        return line.rstrip() if comment is None else line
    rendered = _render(toks)
    return f"{rendered} {comment}" if comment is not None else rendered


def format_source(source: str) -> str:
    """Return the canonically-formatted form of ``source``.

    Pure and line-based: it never raises and never drops a comment. Idempotent —
    ``format_source(format_source(x)) == format_source(x)``."""
    return "\n".join(format_line(ln) for ln in source.splitlines()) + "\n"
