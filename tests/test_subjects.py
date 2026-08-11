"""Unit tests for subject selection (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import subjects  # noqa: E402


def _entry(address, variants, messages=100):
    return {
        "id": address,
        "identifiers": [address] + variants,
        "attributes": {
            "address": address,
            "display_name_variants": variants,
            "exchange_logins": [],
            "domain": address.split("@")[1],
            "internal": address.endswith("@enron.com"),
            "message_count": messages,
            "sent_count": messages,
            "received_count": 0,
            "custodians": [],
            "distinct_correspondents": 0,
            "top_correspondents": [],
        },
        "active_from": "2001-01-01T00:00:00+00:00",
        "active_to": "2001-12-31T00:00:00+00:00",
    }


# --- list context vs description -------------------------------------------- #

def test_a_semicolon_separated_recipient_list_is_list_context():
    window = ("Grigsby, Mike; Martin, Thomas A.; Neal, Scott; Arora, Harry; "
              "Ermis, Frank; Allen, Phillip K.")
    assert subjects.is_list_context(window) is True


def test_a_forwarded_header_block_is_list_context():
    assert subjects.is_list_context("-----Original Message----- From: a@b Sent: Monday")
    assert subjects.is_list_context("Forwarded by Monika Causholli/PDX/ECT on 04/04")


def test_a_sentence_is_not_list_context():
    assert subjects.is_list_context(
        "This week's lunchtime presentation will feature Harry Arora, VP of "
        "eCommerce, on Thursday.") is False


def test_classify_mentions_counts_both_kinds():
    pattern = re.compile("causholli", re.I)
    text = ("Attached is the market wrap for the week of Oct 19. If you have any "
            "questions about the pulp and paper numbers you can contact me. "
            "thanks, Monika Causholli" + " filler." * 30 +
            "-----Original Message----- From: x To: Causholli, Monika; Smith, Ann; "
            "Jones, Bob; Roe, Jane")
    descriptive, listed = subjects.classify_mentions(text, pattern)
    assert descriptive == 1 and listed == 1
    assert subjects.describes_subject(text, pattern) is True


def test_classification_is_conservative_in_short_forwarded_messages():
    """A header marker within the window makes every mention read as list context.

    Undercounting descriptive mentions passes over a usable subject; the
    opposite would score an unusable one.
    """
    text = "thanks, Monika Causholli. -----Original Message----- From: x"
    descriptive, listed = subjects.classify_mentions(text, re.compile("causholli", re.I))
    assert (descriptive, listed) == (0, 1)


def test_a_document_of_pure_list_does_not_describe_the_subject():
    text = "To: Grigsby, Mike; Neal, Scott; Causholli, Monika; Ermis, Frank"
    assert subjects.describes_subject(text, re.compile("causholli", re.I)) is False


def test_near_duplicate_key_collapses_recirculated_announcements():
    a = "Lite Bytz RSVP. This week's presentation features the APPZ speaker."
    b = "Lite Bytz RSVP.   This week's presentation features the APPZ speaker."
    assert subjects.near_duplicate_key(a) == subjects.near_duplicate_key(b)
    assert subjects.near_duplicate_key(a) != subjects.near_duplicate_key("Something else")


# --- name extraction --------------------------------------------------------- #

def test_surname_and_given_name_from_both_orderings():
    e1 = _entry("a@enron.com", ["Causholli, Monika"])
    e2 = _entry("b@enron.com", ["Phillip K Allen"])
    assert subjects.surname_of(e1) == "Causholli"
    assert subjects.given_name_of(e1) == "Monika"
    assert subjects.surname_of(e2) == "Allen"
    assert subjects.given_name_of(e2) == "Phillip"


def test_a_single_token_name_yields_no_surname():
    e = _entry("a@enron.com", ["Reception"])
    assert subjects.surname_of(e) == ""
    assert subjects.given_name_of(e) == ""


# --- the ordinary-word screen ------------------------------------------------ #

def test_word_likeness_uses_measured_ratios():
    assert subjects.is_word_like(14480, 154)   # "love", 94x
    assert subjects.is_word_like(2928, 74)     # "dean", 40x
    assert not subjects.is_word_like(430, 239)      # "causholli", 1.8x
    assert not subjects.is_word_like(11354, 10173)  # "shackleton", 1.1x


def test_word_likeness_tolerates_a_zero_denominator():
    assert subjects.word_likeness(100, 0) == 100.0


class _FakeClient:
    def __init__(self, counts, hits=()):
        self.counts = counts
        self.hits = list(hits)

    def count(self, index, body):
        return {"count": self.counts.get(body["query"]["match_phrase"]["message"], 0)}

    def search(self, index, body):
        return {"hits": {"hits": [{"_source": {"message": t}} for t in self.hits]}}


def test_screen_rejects_a_surname_that_is_an_ordinary_word():
    client = _FakeClient({"Love": 14480, "Phillip Love": 154})
    ok, detail = subjects.screen(client, _entry("phillip.love@enron.com", ["Phillip Love"]))
    assert ok is False
    assert detail["ratio"] == 94.0
    assert "ordinary word" in detail["reason"]


def test_screen_accepts_a_rare_surname():
    client = _FakeClient({"Causholli": 430, "Monika Causholli": 239})
    ok, detail = subjects.screen(
        client, _entry("monika.causholli@enron.com", ["Causholli, Monika"]))
    assert ok is True and detail["reason"] == ""


def test_screen_rejects_a_surname_too_short_to_judge():
    client = _FakeClient({})
    ok, detail = subjects.screen(client, _entry("j.ng@enron.com", ["Ng, Jan"]))
    assert ok is False and "too short" in detail["reason"]


# --- ranking ----------------------------------------------------------------- #

def test_rank_orders_by_distinct_descriptive_and_reports_rejections():
    docs = ["thanks, Causholli", "thanks, Causholli",          # duplicate wording
            "Causholli presented the weekly wrap to the desk",
            "To: Neal, Scott; Causholli, Monika; Ermis, Frank; Roe, Jane"]
    client = _FakeClient({"Causholli": 430, "Monika Causholli": 239,
                          "Love": 14480, "Phillip Love": 154}, hits=docs)
    entries = [_entry("monika.causholli@enron.com", ["Causholli, Monika"]),
               _entry("phillip.love@enron.com", ["Phillip Love"])]
    result = subjects.rank(client, entries, candidates=10)

    assert [r["id"] for r in result["ranked"]] == ["monika.causholli@enron.com"]
    assert [r["id"] for r in result["screened_out"]] == ["phillip.love@enron.com"]
    row = result["ranked"][0]
    assert row["descriptive_documents"] == 3
    assert row["list_only_documents"] == 1
    assert row["distinct_descriptive"] == 2  # the duplicate collapsed


def test_rank_skips_entries_without_a_name_or_enough_volume():
    client = _FakeClient({})
    entries = [_entry("quiet@enron.com", ["Ann Roe"], messages=5),
               _entry("nameless@enron.com", [])]
    result = subjects.rank(client, entries, candidates=10, min_messages=20)
    assert result["pool_size"] == 0
    assert result["ranked"] == []
