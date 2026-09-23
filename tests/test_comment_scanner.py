"""One comment scanner, the lexer's (TD-9).

The lexer, the formatter, the pragma parser, the LSP and the editor all need to
know where a line's comment starts, which means knowing where every ``"..."``
string ends. They used to carry four copies of that scan, and the LSP's ignored
``\\"`` escapes, so it saw a comment inside ``note "say \\" # hi"``. Now all of
them call :func:`barndsl.compiler.comment_start`, built on the lexer's own
:func:`~barndsl.compiler.scan_string`.
"""

from __future__ import annotations

import itertools

import pytest

from barndsl import compile_source, fmt, lsp, pragma
from barndsl.compiler import comment_start, scan_string, tokenize_line


@pytest.mark.parametrize(
    ("line", "cut"),
    [
        ('room a: living at 0,0 size 10 x 10', None),
        ('# a whole-line comment', 0),
        ('note "a # b" # real', 13),
        ('note "say \\" # hi" # real', 19),  # an escaped quote doesn't close it
        ('note "back\\\\" # real', 14),  # an escaped backslash, then the quote closes
        ('note "c:\\tmp # x" # real', 18),  # any other backslash is literal
        ('note "never closes # still text', None),  # an unterminated string runs to the end
        ('note "a" "b # c" # d', 17),
    ],
)
def test_comment_start(line, cut):
    assert comment_start(line) == cut


def test_scan_string_unescapes_only_quotes_and_backslashes():
    line = 'note "say \\"hi\\" c:\\tmp \\\\" at 1,2'
    text, end, closed = scan_string(line, 5)
    assert (text, closed) == ('say "hi" c:\\tmp \\', True)
    assert line[end:] == " at 1,2"
    assert scan_string('"open', 0) == ("open", 5, False)


# Fragments that stress the quote/escape/comment interplay.
_BITS = ['note', ' ', '"', '\\"', '\\\\', '\\', '#', 'x', '"a # b"', ',', '\t', '{', '′']


def test_the_lexer_stops_exactly_where_the_comment_starts():
    # Over every 4-fragment line (28,561 of them): each token the lexer reads lies before the
    # comment, and cutting the line there changes nothing — the scanner and the
    # lexer agree on where every string ends.
    for line in ("".join(p) for p in itertools.product(_BITS, repeat=4)):
        cut = comment_start(line)
        if cut is None:
            continue
        toks = tokenize_line(line, 1)
        assert all(t.end_col - 1 <= cut for t in toks), line
        assert tokenize_line(line[:cut], 1) == toks, line


def test_every_consumer_uses_the_lexers_scanner():
    assert fmt.comment_start is pragma.comment_start is lsp.comment_start is comment_start


def test_the_formatter_keeps_an_escaped_quote_inside_its_string():
    assert fmt.format_line('note   "say \\" # hi"    # real') == 'note "say \\" # hi" # real'


def test_lsp_hover_ignores_a_pragma_lookalike_inside_a_string():
    # The note's text mentions an accept pragma; it isn't one, so hovering the
    # code inside the string explains nothing.
    text = 'plan "P"\nnote "see \\" # accept NAT_LIGHT" at 1,1\n'
    col = text.splitlines()[1].index("NAT_LIGHT")
    assert lsp.hover(text, compile_source(text), 1, col) is None


def test_lsp_finds_a_real_pragma_after_an_escaped_quote():
    # The old scanner ended the string at the escaped quote, so the real pragma
    # looked like it was inside one: no hover, no code completion.
    text = 'plan "P"\nnote "a\\"" at 1,1  # barndsl: accept NAT_LIGHT\n'
    line = text.splitlines()[1]
    h = lsp.hover(text, compile_source(text), 1, line.index("NAT_LIGHT"))
    assert h is not None and "NAT_LIGHT" in h["contents"]["value"]
    items = lsp.completions(text, 1, line.index("NAT_LIGHT"), compile_source(text), None)
    assert "NAT_LIGHT" in {i["label"] for i in items}


def test_lsp_reads_a_use_path_the_way_the_lexer_does(tmp_path):
    (tmp_path / "wing.barn").write_text("room a: living at 0,0 size 10 x 10\n", encoding="utf-8")
    # `\"` doesn't close the path, so the cursor is still inside it: offer parts.
    text = 'use "a\\" '
    items = lsp.completions(text, 0, len(text), compile_source(text), str(tmp_path))
    assert "wing.barn" in {i["label"] for i in items}


def test_a_brief_keeps_a_hash_inside_its_quoted_text():
    from barndsl.layout import parse_brief

    brief = parse_brief('plan "Unit #3"\nnote "see #2 below"   # a real comment\n'
                        "room a: living 12 x 12\n")
    assert brief.name == "Unit #3"
    assert brief.notes == "see #2 below"
