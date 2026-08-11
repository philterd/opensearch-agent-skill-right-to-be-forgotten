"""Unit tests for case-caption parsing (no cluster, no network).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import caselaw  # noqa: E402


def test_a_plain_caption_splits_into_two_parties():
    assert caselaw.parse_caption("STATE of Louisiana v. Lloyd W. ADDISON") == [
        "STATE of Louisiana", "Lloyd W. ADDISON"]


def test_role_designations_are_stripped_before_the_surname():
    """A naive split made "Defendant-Appellant" the most common surname."""
    caption = "Smith, Defendant-Appellant v. Jones, Plaintiff-Appellee"
    assert caselaw.parse_caption(caption) == ["Smith", "Jones"]
    assert caselaw.party_surnames(caption) == ["Smith", "Jones"]


def test_the_junk_surnames_the_naive_parser_produced_are_gone():
    for junk in ("Doe, Appellant", "Roe, Respondent", "Fox, Defendant-Appellant",
                 "Lee, Petitioner, et al."):
        assert caselaw.surname_of(caselaw.parse_caption(junk + " v. Other")[0]) not in (
            "Appellant", "Respondent", "Defendant-Appellant", "Petitioner", "al")


def test_vs_and_v_are_both_separators():
    assert len(caselaw.parse_caption("Alpha vs. Beta")) == 2
    assert len(caselaw.parse_caption("Alpha v Beta")) == 2


def test_a_relator_behind_an_institutional_party_is_recovered():
    """"State of Tennessee, ex rel. Latonya Campbell" names a real person."""
    assert caselaw.party_surnames(
        "State of Tennessee, ex rel. Latonya Campbell v. Thomas Conley") == [
            "Campbell", "Conley"]


def test_in_re_and_ex_parte_prefixes_are_dropped():
    assert caselaw.parse_caption("In re Marriage of Hannah Vance") == [
        "Marriage of Hannah Vance"]
    assert caselaw.party_surnames("Ex parte Michael Perna") == ["Perna"]


def test_institutional_parties_yield_no_surname():
    for caption in ("United States v. Kowalski", "State of Tennessee v. Perna",
                    "Acme Insurance Company v. Ruiz"):
        surnames = caselaw.party_surnames(caption)
        assert surnames and all(s.lower() not in caselaw.GENERIC for s in surnames)
    assert caselaw.party_surnames("United States v. State of Ohio") == []


def test_an_organisation_yields_no_person_surname():
    """Companies are not data subjects; the harness wants people."""
    for org in ("Widget Holdings, Inc.", "Acme Corp", "Bank of America",
                "United States", "County of Kern"):
        assert caselaw.surname_of(org) == ""
        assert caselaw.is_organisation(org) is True
    assert caselaw.is_organisation("Lloyd W. Addison") is False


def test_personal_suffixes_are_removed():
    assert caselaw.surname_of("Thomas Conley, Jr.") == "Conley"
    assert caselaw.surname_of("Ada Byron III") == "Byron"


def test_a_short_or_non_alphabetic_surname_is_rejected():
    assert caselaw.surname_of("John Ng") == ""          # too short to mask on
    assert caselaw.surname_of("Docket No. 12-3456") == ""


def test_surnames_are_deduplicated():
    assert caselaw.party_surnames("Campbell v. Campbell") == ["Campbell"]


def test_given_names_widen_the_alias_set():
    """Masking only the caption surname left "The Defendant, Michael Ray ,"."""
    assert caselaw.given_names("State of Tennessee v. Michael Ray Perna") == ["Michael"]
    assert "Lloyd" in caselaw.given_names("STATE of Louisiana v. Lloyd W. ADDISON")


def test_empty_and_malformed_captions_do_not_raise():
    for bad in ("", None, "   ", "v.", "In re", ",,,"):
        assert caselaw.parse_caption(bad) == [] or all(
            isinstance(p, str) for p in caselaw.parse_caption(bad))
        assert caselaw.party_surnames(bad) == []


def test_real_captions_from_the_sample():
    cases = {
        "State of Tennessee, ex rel. Latonya Campbell v. Thomas Conley": ["Campbell", "Conley"],
        "STATE of Louisiana v. Lloyd W. ADDISON": ["ADDISON"],
        "State of Tennessee v. Michael Ray Perna": ["Perna"],
    }
    for caption, expected in cases.items():
        assert caselaw.party_surnames(caption) == expected


# --- compound captions, from real CourtListener data ------------------------- #

def test_a_person_with_an_institution_appended_keeps_the_person():
    """One caption string can hold two parties, comma delimited."""
    assert caselaw.party_surnames(
        "United States v. James E. Gaines, United States of America") == ["Gaines"]


def test_a_trailing_office_and_place_are_trimmed():
    assert caselaw.party_surnames(
        "Alexander M. Hunter, District Attorney, Twentieth Judicial District, "
        "Boulder, Colorado v. District Court") == ["Hunter"]


def test_an_office_title_is_not_a_surname():
    assert caselaw.party_surnames(
        "Samson TEKLEWOLD, Petitioner, v. Alberto R. GONZALES, Attorney General, "
        "Respondent") == ["TEKLEWOLD", "GONZALES"]


def test_a_leading_article_does_not_hide_an_institution():
    """"the STATE of Texas" is headed by "the", not by "state"."""
    assert caselaw.party_surnames(
        "Juan RODRIGUEZ, Appellant, v. the STATE of Texas, Appellee") == ["RODRIGUEZ"]


def test_an_institution_inside_the_first_segment_yields_nothing_usable():
    assert caselaw.party_surnames("Ward v. British Government General") == ["Ward"]
    assert caselaw.surname_of("British Government General") == ""


def test_a_single_token_party_is_still_a_person():
    assert caselaw.party_surnames("People v. Johnson") == ["Johnson"]
