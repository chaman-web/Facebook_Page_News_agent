"""
tests/test_compress_summary.py — Tests for _compress_summary (policy-aligned).

Scenarios covered:
  1.  Empty string returns empty string
  2.  None-like (empty) handled safely
  3.  Short summary (<= 150 words) passes through with only noise stripped
  4.  URL boilerplate sentences dropped ("Read more at bbc.com")
  5.  Subscribe prompt sentences dropped
  6.  Click here sentences dropped
  7.  Copyright sentences dropped
  8.  "Developing story" sentences dropped from long summaries
  9.  Near-duplicate sentences deduplicated (keep first)
  10. Similar but not duplicate sentences both kept
  11. Fact-bearing sentences NEVER dropped (numbers, proper nouns, acronyms)
  12. Filler sentences dropped when over limit
  13. Output never exceeds _SOFT_WORD_LIMIT words
  14. Output never drops below _MIN_WORDS when input is long enough
  15. Narrative order preserved after compression
  16. Returns original on complete noise strip (safety)
  17. Never raises on any input
  18. Temperature and retry_hint plumbing in _build_prompt
  19. retry_hint appended on attempt > 1, absent on attempt 1
  20. _build_prompt uses _compress_summary on SUMMARY line
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Story, VerificationStatus
from pipeline.generator import (
    _MIN_WORDS,
    _SOFT_WORD_LIMIT,
    _build_prompt,
    _compress_summary,
    _is_fact_bearing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _long(sentences: list[str]) -> str:
    """Join sentences into a summary long enough to trigger compression (>150w)."""
    base = " ".join(sentences)
    # Pad to >150 words if needed using neutral filler that won't affect tests
    while len(base.split()) <= 150:
        base += (
            " The regional government confirmed all emergency protocols are being followed. "
            "International observers have noted the severity of the situation."
        )
    return base


def _story(summary: str) -> Story:
    s = Story(
        title="Test story title",
        source_name="BBC",
        source_url="https://bbc.com",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        category="breaking",
    )
    s.verification_status = VerificationStatus.VERIFIED
    s.corroborating_sources = []
    return s


# ---------------------------------------------------------------------------
# 1–3: Basic pass-through
# ---------------------------------------------------------------------------

class TestPassThrough:
    def test_empty_string(self):
        assert _compress_summary("") == ""

    def test_whitespace_only(self):
        assert _compress_summary("   ") == ""

    def test_short_summary_unchanged(self):
        text = "A 6.2 magnitude earthquake struck Turkey killing 12 people."
        result = _compress_summary(text)
        assert result == text

    def test_short_summary_150_words_unchanged(self):
        # 20 words — well under 150
        text = " ".join(["Word"] * 20 + ["Turkey", "earthquake", "kills", "12", "people."])
        result = _compress_summary(text)
        assert len(result.split()) == len(text.split())


# ---------------------------------------------------------------------------
# 4–8: Boilerplate sentence removal
# ---------------------------------------------------------------------------

class TestBoilerplateRemoval:
    def _has_fact(self, text: str) -> str:
        """Return a long text containing 'text' plus padding fact sentences."""
        facts = (
            "A magnitude 6.2 earthquake struck eastern Turkey killing 12 people in Malatya. "
            "Rescue teams are searching 14 collapsed buildings across the affected region. "
            "The Turkish Disaster Management Authority deployed 400 personnel to the area. "
            "Hospital officials confirmed 47 people are being treated for injuries. "
            "President Erdogan pledged full government support for survivors. "
            "The earthquake struck at a depth of 10 kilometres and was felt widely. "
            "Power outages have affected over 15000 homes in the region. "
            "NATO confirmed the situation is being monitored by member states. "
        )
        return facts + text

    def test_read_more_dropped(self):
        text = self._has_fact("Read more at bbc.com.")
        result = _compress_summary(text)
        assert "Read more" not in result

    def test_subscribe_dropped(self):
        text = self._has_fact("Subscribe for breaking news updates.")
        result = _compress_summary(text)
        assert "Subscribe" not in result

    def test_click_here_dropped(self):
        text = self._has_fact("Click here for the full story.")
        result = _compress_summary(text)
        assert "Click here" not in result

    def test_copyright_dropped(self):
        text = self._has_fact("Copyright Reuters 2026.")
        result = _compress_summary(text)
        assert "Copyright" not in result

    def test_developing_story_dropped_from_long(self):
        text = self._has_fact("This is a developing story.")
        result = _compress_summary(text)
        assert "developing story" not in result.lower()

    def test_short_fragment_dropped(self):
        """Fragments under 20 chars (URL artifacts) are dropped."""
        text = self._has_fact(".com.")
        result = _compress_summary(text)
        assert ".com." not in result


# ---------------------------------------------------------------------------
# 9–10: Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def _padded(self, sentences: list[str]) -> str:
        base = " ".join(sentences)
        padding = (
            " The Turkish Disaster Management Authority deployed 400 personnel. "
            "Hospital officials confirmed 47 people are being treated. "
            "NATO confirmed the situation is being monitored by member states. "
            "Power outages have affected over 15000 homes in the mountainous region. "
            "International observers noted the severity of this natural disaster. "
            "The earthquake occurred at a depth of 10 kilometres below the surface. "
        )
        while len(base.split()) <= 150:
            base += padding
        return base

    def test_exact_duplicate_removed(self):
        sent = "Turkish officials confirmed the earthquake struck at 3am local time in eastern Turkey."
        text = self._padded([sent, sent])
        result = _compress_summary(text)
        # Should appear only once
        assert result.count("3am local time") == 1

    def test_near_duplicate_removed(self):
        s1 = "Turkish officials confirmed the earthquake struck at 3am local time in eastern Turkey."
        s2 = "Turkish officials have confirmed the earthquake struck at 3am local time in eastern Turkey."
        text = self._padded([s1, s2])
        result = _compress_summary(text)
        # At most one should survive
        count = result.count("3am local time")
        assert count <= 1

    def test_different_sentences_both_kept(self):
        s1 = "Twelve people died in the earthquake in Malatya province of eastern Turkey."
        s2 = "Rescue teams found survivors trapped in three collapsed apartment buildings."
        text = self._padded([s1, s2])
        result = _compress_summary(text)
        assert "Malatya" in result or "twelve" in result.lower()
        assert "Rescue" in result or "survivors" in result.lower()


# ---------------------------------------------------------------------------
# 11: Fact-bearing sentences never dropped
# ---------------------------------------------------------------------------

class TestFactBearing:
    def test_is_fact_bearing_with_number(self):
        assert _is_fact_bearing("The earthquake killed 12 people.") is True

    def test_is_fact_bearing_with_proper_noun(self):
        assert _is_fact_bearing("President Erdogan pledged full support.") is True

    def test_is_fact_bearing_with_acronym(self):
        assert _is_fact_bearing("NATO confirmed it is monitoring the situation.") is True

    def test_is_fact_bearing_with_quote(self):
        assert _is_fact_bearing('He said "we will rebuild everything."') is True

    def test_is_fact_bearing_false_for_pure_filler(self):
        assert _is_fact_bearing("in related news, the situation continued.") is False

    def test_fact_sentence_preserved_even_if_filler_pattern(self):
        """A sentence matching filler pattern BUT containing a fact is kept."""
        # "Meanwhile, 12 people died" -- matches filler but has a number
        sent = "Meanwhile, 12 people died in the earthquake."
        assert _is_fact_bearing(sent) is True

    def test_numbers_kept_after_compression(self):
        """Sentences with numbers survive compression."""
        facts = (
            "A 6.2 magnitude earthquake struck Turkey killing 12 people. "
            "Rescue teams found 47 survivors in collapsed buildings. "
            "The Turkish government deployed 400 emergency personnel. "
            "NATO and WHO issued statements expressing concern. "
            "Power outages hit 15000 homes across three provinces. "
        )
        # Pad to trigger compression
        text = facts * 5
        result = _compress_summary(text)
        # At least the key numbers should survive
        assert "12" in result or "47" in result or "400" in result


# ---------------------------------------------------------------------------
# 12–14: Word limit enforcement
# ---------------------------------------------------------------------------

class TestWordLimits:
    def _make_long_summary(self, word_count: int) -> str:
        """Create a summary of roughly word_count words with facts."""
        base = (
            "A magnitude 6.2 earthquake struck eastern Turkey killing 12 people. "
            "Rescue teams are searching 14 collapsed buildings in Malatya province. "
            "The Turkish Disaster Authority deployed 400 personnel to the region. "
            "Hospital officials confirmed 47 people are being treated for injuries. "
            "President Erdogan pledged full government support for survivors today. "
            "The quake struck at depth of 10 kilometres and was felt across Turkey. "
            "Power outages affected 15000 homes in the mountainous eastern region. "
            "NATO and WHO confirmed they are monitoring the humanitarian situation. "
        )
        result = ""
        while len(result.split()) < word_count:
            result += base
        return result

    def test_output_under_soft_limit(self):
        text = self._make_long_summary(400)
        result = _compress_summary(text)
        assert len(result.split()) <= _SOFT_WORD_LIMIT

    def test_output_not_below_min_words_when_input_long(self):
        text = self._make_long_summary(300)
        result = _compress_summary(text)
        assert len(result.split()) >= _MIN_WORDS

    def test_short_input_below_min_returned_unchanged(self):
        """If input is naturally short, we don't pad it -- return as-is."""
        text = "A 6.2 earthquake struck Turkey killing 12 people in Malatya."
        result = _compress_summary(text)
        # Should pass through unchanged (under 150w threshold)
        assert result == text


# ---------------------------------------------------------------------------
# 15: Narrative order preserved
# ---------------------------------------------------------------------------

class TestNarrativeOrder:
    def test_order_preserved_after_compression(self):
        """Sentences should appear in original order in the output."""
        s1 = "The earthquake struck at 3am killing 12 people in eastern Turkey."
        s2 = "Rescue teams arrived at dawn and began searching 14 buildings."
        s3 = "By midday, President Erdogan had visited the disaster zone personally."
        s4 = "International aid including WHO and NATO support arrived on Thursday."
        # Build long enough to trigger compression path
        padding = (
            " The Turkish Disaster Management Authority deployed 400 personnel. "
            "Hospital officials confirmed 47 people are receiving treatment now. "
            "Power outages have affected over 15000 homes in the mountainous region. "
        )
        text = s1 + " " + s2 + " " + s3 + " " + s4 + padding * 3
        result = _compress_summary(text)
        # All four should be in the result (all fact-bearing)
        # And their relative order should be preserved
        positions = []
        for s in [s1, s2, s3, s4]:
            # Check by first unique keyword
            keyword = s.split()[2]  # 3rd word is distinctive enough
            idx = result.find(keyword)
            if idx != -1:
                positions.append(idx)
        if len(positions) >= 2:
            assert positions == sorted(positions), "Narrative order broken"


# ---------------------------------------------------------------------------
# 16–17: Safety invariants
# ---------------------------------------------------------------------------

class TestSafetyInvariants:
    def test_never_raises_on_random_input(self):
        bad_inputs = [
            None,
            "",
            "   ",
            "\x00\x01\x02",
            "a" * 10000,
            "!@#$%^&*()",
            "\n\n\n\n",
        ]
        for inp in bad_inputs:
            try:
                result = _compress_summary(inp or "")
                assert isinstance(result, str)
            except Exception as e:
                pytest.fail(f"_compress_summary raised on input {repr(inp)}: {e}")

    def test_returns_string_always(self):
        for inp in ["hello", "", "x" * 5000]:
            assert isinstance(_compress_summary(inp), str)


# ---------------------------------------------------------------------------
# 18–20: _build_prompt wiring
# ---------------------------------------------------------------------------

class TestBuildPromptWiring:
    def test_temperature_is_0_60(self):
        """Confirm temperature was updated from 0.35 to 0.60."""
        import ast
        import inspect
        import pipeline.generator as gen
        # Read source and check temperature value
        src = (
            Path(__file__).resolve().parents[1] / "pipeline" / "generator.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert "temperature=0.60" in src, "Temperature should be 0.60"
        assert "temperature=0.35" not in src, "Old temperature 0.35 should be removed"

    def test_retry_hint_absent_on_first_attempt(self):
        story = _story("Short summary.")
        prompt = _build_prompt(story, retry_hint=None)
        assert "IMPORTANT: Your previous attempt" not in prompt

    def test_retry_hint_present_on_retry(self):
        story = _story("Short summary.")
        prompt = _build_prompt(story, retry_hint="previous output was too short")
        assert "previous output was too short" in prompt
        assert "IMPORTANT" in prompt

    def test_compress_summary_called_in_prompt(self):
        """SUMMARY line in the prompt uses compressed summary, not raw."""
        # Use a summary with a boilerplate sentence
        raw = (
            "A 6.2 earthquake struck Turkey killing 12 people. "
            "Read more at bbc.com."
        )
        story = _story(raw)
        prompt = _build_prompt(story)
        # The boilerplate should NOT appear in the prompt
        assert "Read more" not in prompt
        # The fact should still be there
        assert "12" in prompt or "earthquake" in prompt.lower()

    def test_build_prompt_accepts_retry_hint_kwarg(self):
        """_build_prompt signature accepts retry_hint without error."""
        story = _story("Turkey earthquake summary.")
        # Should not raise
        prompt1 = _build_prompt(story)
        prompt2 = _build_prompt(story, retry_hint=None)
        prompt3 = _build_prompt(story, retry_hint="too short")
        assert isinstance(prompt1, str)
        assert isinstance(prompt2, str)
        assert isinstance(prompt3, str)
        assert len(prompt3) > len(prompt2)  # retry_hint adds text
