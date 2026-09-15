"""
tests/test_hashtag_generation.py — Tests for the hybrid hashtag system.

Scenarios:
  1.  LLM gives 4 valid tags → used, #GlobalPulseNews prepended
  2.  LLM gives 2 valid tags → used (minimum threshold met)
  3.  LLM gives only 1 valid tag → fallback to deterministic
  4.  LLM gives 0 valid tags → fallback to deterministic
  5.  LLM HASHTAGS block missing entirely → fallback to deterministic
  6.  LLM tags capped at 4 even if LLM gives more
  7.  #GlobalPulseNews from LLM is blocked (added automatically)
  8.  Generic blocked tags filtered out (#News, #Update, #Breaking, etc.)
  9.  Invented compound tags with 3+ caps filtered (#MuslimDemocrats)
  10. Valid single-word tags accepted (#Gaza, #Trump, #BRICS)
  11. Valid mixed-case tags accepted (#CelineDion, #SaudiArabia)
  12. Tag longer than 30 chars rejected
  13. Tag with spaces inside rejected
  14. _parse_llm_hashtags never raises on any input
  15. Deterministic fallback fix: #WHO not matched by pronoun "who"
  16. Deterministic fallback fix: #WHO matched by uppercase "WHO"
  17. Deterministic fallback: always starts with #GlobalPulseNews
  18. Deterministic fallback: caps at 5 total tags
  19. Full generate_post flow uses LLM tags when valid
  20. Full generate_post flow uses deterministic when LLM fails
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Story, VerificationStatus
from pipeline.generator import _generate_hashtags, _parse_llm_hashtags


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _story(
    title: str = "Earthquake kills 12 people in Turkey",
    summary: str = "A powerful earthquake killed 12 people in Turkey.",
    category: str = "breaking",
) -> Story:
    s = Story(
        title=title,
        source_name="BBC",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        category=category,
    )
    s.verification_status = VerificationStatus.VERIFIED
    return s


def _raw_with_hashtags(tags: str) -> str:
    return (
        "CARD_HEADLINES:\n1. Headline one\n2. Headline two\n3. Headline three\n\n"
        "CARD_DESCRIPTION:\nA verified detail about this story.\n\n"
        f"HASHTAGS:\n{tags}\n\n"
        "POST:\nPost body text here."
    )


# ---------------------------------------------------------------------------
# 1–6: LLM tag parsing — count thresholds
# ---------------------------------------------------------------------------

class TestLLMTagCount:
    def test_4_valid_tags_all_used(self):
        raw = _raw_with_hashtags("#Gaza #Israel #MiddleEast #Ceasefire")
        result = _parse_llm_hashtags(raw)
        assert len(result) == 4
        assert "#Gaza" in result
        assert "#Ceasefire" in result

    def test_2_valid_tags_meets_minimum(self):
        raw = _raw_with_hashtags("#Gaza #Israel")
        result = _parse_llm_hashtags(raw)
        assert len(result) == 2

    def test_1_valid_tag_below_minimum(self):
        """1 valid tag — _parse_llm_hashtags returns it, caller decides to fallback."""
        raw = _raw_with_hashtags("#Gaza")
        result = _parse_llm_hashtags(raw)
        assert len(result) == 1

    def test_no_valid_tags_returns_empty(self):
        raw = _raw_with_hashtags("#News #Update #Breaking #Latest")
        result = _parse_llm_hashtags(raw)
        assert result == []

    def test_missing_hashtags_block_returns_empty(self):
        raw = "CARD_HEADLINES:\n1. A\n\nCARD_DESCRIPTION:\nDesc.\n\nPOST:\nBody"
        result = _parse_llm_hashtags(raw)
        assert result == []

    def test_capped_at_4_even_if_llm_gives_more(self):
        raw = _raw_with_hashtags("#Gaza #Israel #MiddleEast #Ceasefire #Palestine #Hamas")
        result = _parse_llm_hashtags(raw)
        assert len(result) <= 4


# ---------------------------------------------------------------------------
# 7–12: Tag filtering rules
# ---------------------------------------------------------------------------

class TestTagFiltering:
    def test_globalpulsenews_blocked(self):
        raw = _raw_with_hashtags("#GlobalPulseNews #Gaza #Israel #MiddleEast")
        result = _parse_llm_hashtags(raw)
        assert "#GlobalPulseNews" not in result
        assert "#globalpulsenews" not in [t.lower() for t in result]

    def test_generic_news_blocked(self):
        for tag in ["#News", "#Update", "#Latest", "#Breaking", "#Trending"]:
            raw = _raw_with_hashtags(f"{tag} #Gaza #Israel #MiddleEast")
            result = _parse_llm_hashtags(raw)
            assert tag not in result, f"{tag} should be blocked"

    def test_worldnews_and_globalnews_blocked(self):
        raw = _raw_with_hashtags("#WorldNews #GlobalNews #Gaza #Israel")
        result = _parse_llm_hashtags(raw)
        assert "#WorldNews" not in result
        assert "#GlobalNews" not in result

    def test_invented_compound_tag_filtered(self):
        """#MuslimDemocrats has 3+ uppercase letters and is long — filtered."""
        raw = _raw_with_hashtags("#MuslimDemocrats #Politics #USA #Elections")
        result = _parse_llm_hashtags(raw)
        assert "#MuslimDemocrats" not in result

    def test_valid_single_word_tags_accepted(self):
        for tag in ["#Gaza", "#Trump", "#BRICS", "#Ukraine", "#Pakistan"]:
            raw = _raw_with_hashtags(f"{tag} #Israel #MiddleEast #Ceasefire")
            result = _parse_llm_hashtags(raw)
            assert tag in result, f"{tag} should be accepted"

    def test_valid_mixed_case_accepted(self):
        raw = _raw_with_hashtags("#CelineDion #France #Entertainment #Music")
        result = _parse_llm_hashtags(raw)
        assert "#CelineDion" in result

    def test_tag_over_30_chars_rejected(self):
        long_tag = "#" + "A" * 31
        raw = _raw_with_hashtags(f"{long_tag} #Gaza #Israel #MiddleEast")
        result = _parse_llm_hashtags(raw)
        assert long_tag not in result

    def test_tag_with_space_inside_rejected(self):
        raw = _raw_with_hashtags("#Middle East #Gaza #Israel #Ceasefire")
        result = _parse_llm_hashtags(raw)
        assert "#Middle" in result or "#MiddleEast" not in result
        # Main point: "#Middle East" should not appear as one tag
        assert "#Middle East" not in result


# ---------------------------------------------------------------------------
# 14: _parse_llm_hashtags never raises
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_empty_string(self):
        assert _parse_llm_hashtags("") == []

    def test_none_like_input(self):
        assert _parse_llm_hashtags("HASHTAGS:\n") == []

    def test_garbage_input(self):
        assert _parse_llm_hashtags("not valid at all !!!") == []

    def test_malformed_tags(self):
        result = _parse_llm_hashtags("HASHTAGS:\n### ##$ #123 # ")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# 15–18: Deterministic generator fixes
# ---------------------------------------------------------------------------

class TestDeterministicGenerator:
    def test_who_not_matched_by_pronoun(self):
        """Stories containing the word 'who' (pronoun) must NOT get #WHO."""
        story = _story(
            title="Officials who warned about flooding were ignored",
            summary="The officials who issued warnings were not heeded by authorities.",
        )
        tags = _generate_hashtags(story)
        assert "#WHO" not in tags

    def test_who_matched_by_uppercase(self):
        """Stories mentioning uppercase WHO should get #WHO."""
        story = _story(
            title="WHO Declares Mpox Outbreak a Global Emergency",
            summary="The WHO declared the mpox outbreak a global health emergency.",
        )
        tags = _generate_hashtags(story)
        assert "#WHO" in tags

    def test_always_starts_with_globalpulsenews(self):
        story = _story()
        tags = _generate_hashtags(story)
        assert tags[0] == "#GlobalPulseNews"

    def test_capped_at_5_total(self):
        story = _story(
            title="Trump visits Pakistan India Kashmir Ukraine Russia China",
            summary="Trump visited multiple countries in a whirlwind diplomatic tour.",
        )
        tags = _generate_hashtags(story)
        assert len(tags) <= 5

    def test_no_invented_compound_tags(self):
        """After removing entity extraction, no CamelCase compound tags."""
        story = _story(
            title="Muslim Democrats face opposition from Republican lawmakers",
            summary="Muslim Democratic lawmakers are facing opposition.",
            category="politics",
        )
        tags = _generate_hashtags(story)
        # Should not contain the invented compound
        assert "#MuslimDemocrats" not in tags

    def test_minimum_3_tags_always_returned(self):
        """Even an obscure story gets at least 3 tags."""
        story = _story(
            title="Local council approves new parking regulations",
            summary="A local council approved new parking rules.",
            category="world",
        )
        tags = _generate_hashtags(story)
        assert len(tags) >= 3


# ---------------------------------------------------------------------------
# 19–20: Hybrid logic in generate_post
# ---------------------------------------------------------------------------

class TestHybridHashtagInGeneratePost:
    def _make_llm_response(self, hashtag_line: str) -> str:
        return (
            "CARD_HEADLINES:\n"
            "1. Earthquake Kills Twelve People in Turkey\n"
            "2. Turkey Earthquake Leaves Twelve Dead\n"
            "3. Powerful Quake Kills Twelve in Turkey\n\n"
            "CARD_DESCRIPTION:\nTwelve people died in a powerful Turkish earthquake.\n\n"
            f"HASHTAGS:\n{hashtag_line}\n\n"
            "POST:\n🔴 BREAKING: A powerful earthquake killed 12 people in Turkey.\n\n"
            "Why this matters: Dozens of buildings collapsed in eastern Turkey.\n\n"
            "Sources: BBC News"
        )

    def test_llm_tags_used_when_valid(self):
        """When LLM provides 2+ valid tags, they are used with #GlobalPulseNews prepended."""
        from pipeline.generator import generate_post

        story = _story()
        story.corroborating_sources = []

        llm_response = self._make_llm_response("#Earthquake #Turkey #NaturalDisaster #Disaster")

        mock_response = MagicMock()
        mock_response.choices[0].message.content = llm_response

        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = mock_response
            result = generate_post(story)

        assert result.hashtags[0] == "#GlobalPulseNews"
        assert "#Earthquake" in result.hashtags or "#Turkey" in result.hashtags

    def test_deterministic_fallback_when_llm_tags_insufficient(self):
        """When LLM provides fewer than 2 valid tags, deterministic generator is used."""
        from pipeline.generator import generate_post

        story = _story()
        story.corroborating_sources = []

        # LLM gives only generic blocked tags
        llm_response = self._make_llm_response("#News #Update")

        mock_response = MagicMock()
        mock_response.choices[0].message.content = llm_response

        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = mock_response
            result = generate_post(story)

        # Deterministic fallback always starts with #GlobalPulseNews
        assert result.hashtags[0] == "#GlobalPulseNews"
        # And produces at least 3 tags
        assert len(result.hashtags) >= 3
