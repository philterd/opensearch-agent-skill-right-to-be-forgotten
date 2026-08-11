"""Unit tests for Enron message parsing (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys
from email.header import Header
from email.message import Message

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_enron  # noqa: E402


def test_header_returns_collapsed_text():
    msg = Message()
    msg["X-To"] = "Blair,   Lynn\n </O=ENRON/CN=Lblair>"
    assert seed_enron._header(msg, "X-To") == "Blair, Lynn </O=ENRON/CN=Lblair>"


def test_header_is_empty_when_absent():
    assert seed_enron._header(Message(), "X-cc") == ""


def test_header_survives_a_non_string_header_object():
    """One encoded header used to abort the whole seeding run.

    Under compat32 a header with encoded words comes back as an
    email.header.Header, which has no .split().
    """
    msg = Message()
    msg["X-From"] = Header("Muller, Marta", "utf-8")
    assert seed_enron._header(msg, "X-From") == "Muller, Marta"


def test_addresses_split_on_commas():
    msg = Message()
    msg["To"] = "a@enron.com, b@enron.com"
    assert seed_enron._addresses(msg, "To") == ["a@enron.com", "b@enron.com"]


def test_addresses_survive_a_non_string_header_object():
    msg = Message()
    msg["To"] = Header("a@enron.com, b@enron.com", "utf-8")
    assert seed_enron._addresses(msg, "To") == ["a@enron.com", "b@enron.com"]


def test_iter_documents_skips_an_unparseable_member_and_records_it(tmp_path, monkeypatch):
    """One bad message must not abort a half-million-message ingest."""
    calls = {"n": 0}

    def exploding_parse(raw, custodian, folder, max_chars):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")
        return {"message": "fine", "custodian": custodian}

    class _Member:
        def __init__(self, name):
            self.name = name

        def isfile(self):
            return True

    members = [_Member("maildir/allen-p/sent/1"), _Member("maildir/allen-p/sent/2")]

    class _Tar:
        def __iter__(self):
            return iter(members)

        def extractfile(self, member):
            import io
            return io.BytesIO(b"raw")

    monkeypatch.setattr(seed_enron, "parse_member", exploding_parse)
    monkeypatch.setattr(seed_enron, "_open_stream", lambda src: (_Tar(), lambda: None))

    skipped = []
    docs = list(seed_enron.iter_documents(source="x", limit=10, skipped=skipped))
    assert [d[0] for d in docs] == ["allen-p/sent/2"]
    assert skipped == ["maildir/allen-p/sent/1"]


def test_parse_member_indexes_the_x_headers():
    raw = (b"Message-ID: <123.JavaMail.evans@thyme>\r\n"
           b"Date: Wed, 7 Nov 2001 09:00:00 -0800 (PST)\r\n"
           b"From: fran.fagan@enron.com\r\n"
           b"To: lynn.blair@enron.com\r\n"
           b"Subject: schedule\r\n"
           b"X-From: Fagan, Fran </O=ENRON/OU=NA/CN=RECIPIENTS/CN=FFAGAN>\r\n"
           b"X-To: Blair, Lynn </O=ENRON/OU=NA/CN=RECIPIENTS/CN=Lblair>\r\n"
           b"\r\n"
           b"Please review the attached schedule.\r\n")
    doc = seed_enron.parse_member(raw, "blair-l", "inbox", 4000)
    assert doc["from"] == "fran.fagan@enron.com"
    assert doc["x_from"] == "Fagan, Fran </O=ENRON/OU=NA/CN=RECIPIENTS/CN=FFAGAN>"
    assert doc["x_to"] == "Blair, Lynn </O=ENRON/OU=NA/CN=RECIPIENTS/CN=Lblair>"
    assert doc["x_cc"] == ""
    # The searched field carries subject and body only, never the headers.
    assert doc["message"].startswith("schedule.")
    assert "Fagan" not in doc["message"]
