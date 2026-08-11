"""Every source file must parse on the oldest Python the project supports.

pyproject declares requires-python >=3.11, but a developer on 3.12 can write
syntax 3.11 rejects and see nothing wrong. PEP 701 reusing the enclosing quote
inside an f-string is the easy way in, and CI was the only thing catching it.

`ast.parse(..., feature_version=(3, 11))` does not help: feature_version gates
some grammar but not the 3.12 f-string tokenizer, so it accepts PEP 701 happily.
This walks the token stream instead.
"""

import io
import pathlib
import re
import sys
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP = {".venv", "__pycache__", ".git", ".pytest_cache"}


def python_floor():
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'requires-python\s*=\s*"[^0-9]*(\d+)\.(\d+)', text)
    assert match, "pyproject.toml declares no requires-python floor"
    return int(match.group(1)), int(match.group(2))


def sources():
    return [p for p in sorted(ROOT.rglob("*.py")) if not SKIP & set(p.parts)]


def nested_quote_fstrings(source):
    """Line numbers where an f-string reuses its own quote character.

    Only 3.12 and later accept it.
    """
    hits, open_fstrings = [], []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    try:
        for token in tokens:
            name = tokenize.tok_name[token.type]
            if name == "FSTRING_START":
                open_fstrings.append(token.string[-1])
            elif name == "FSTRING_END":
                if open_fstrings:
                    open_fstrings.pop()
            elif open_fstrings and name == "STRING" and token.string[:1] == open_fstrings[-1]:
                hits.append(token.start[0])
    except (tokenize.TokenError, SyntaxError):
        pass  # unparseable source is the other test's problem
    return hits


# The detector reads FSTRING_START tokens, which only exist from 3.12. On an
# older interpreter the syntax would have failed at import anyway.
needs_312 = pytest.mark.skipif(sys.version_info < (3, 12),
                               reason="f-string tokens require Python 3.12")


@needs_312
def test_no_source_uses_f_string_syntax_newer_than_the_floor():
    assert python_floor() < (3, 12), "floor moved to 3.12; this check can go"
    failures = []
    for path in sources():
        for line in nested_quote_fstrings(path.read_text()):
            failures.append(f"{path.relative_to(ROOT)}:{line}")
    assert not failures, (
        "f-string reuses its enclosing quote, which Python 3.11 rejects:\n"
        + "\n".join(failures))


@needs_312
def test_the_check_detects_the_syntax_it_is_guarding_against():
    """Without this, the guard could silently stop working."""
    assert nested_quote_fstrings('x = f"{"a"}"\n') == [1]
    assert nested_quote_fstrings("y = f'{\"a\"}'\n") == []
    assert nested_quote_fstrings('z = f"plain {v}"\nw = "ordinary"\n') == []


def test_the_scan_actually_covers_the_sources():
    found = {p.relative_to(ROOT).as_posix() for p in sources()}
    assert "scripts/forget_me.py" in found
    assert "scripts/lib/masking.py" in found
    assert any(f.startswith("tests/") for f in found)
