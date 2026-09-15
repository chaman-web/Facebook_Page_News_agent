"""
tests/test_hashtag_relevance.py — Tests for hashtag relevance and title-first strategy.

Scenarios:
  1.  Title-matched tags come before summary-only tags
  2.  Category tags come before summary-only tags (bleed prevention)
  3.  Summary-only tags used as last resort only (min 3 enforcement)
  4.  Multi-story blurb: France/FederalReserve demoted behind category tags
  5.  Silicon Valley story: no invented compound tags like #USDefense
  6.  Jeremy Thomas: #Entertainment before #China/#Japan
  7.  Trump/Zelenskyy: title-matched tags dominate
  8.  Yemen: title-matched tags dominate
  9.  WHO (uppercase): matched correctly, not on pronoun "who"
  10. WHO (pronoun): NOT matched by story containing "officials who warned"
  11. Compound filter: #USDefense (len 9) blocked
  12. Compound filter: #WarInIran (len 9) blocked
  13. Compound filter: #BRICS (len 5, all caps) NOT blocked
  14. Compound filter: #NATO (len 4) NOT blocked
  15. Known two-word tags allowed: #SaudiArabia, #MiddleEast, #CelineDion
  16. Always starts with #GlobalPulseNews
  17. Never exceeds 5 tags total
  18. Always returns at least 3 tags
  19. LLM compound filter catches #USDefense, #WarInIran
  20. LLM compound filter does not block #BRICS, #NATO, #Trump
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Story, VerificationStatus
from pipeline.generator import _generate_hashtags, _parse_llm_hashtags


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _story(
    title: str,
    summary: str,
    category: str = "politics",
) -> Story:
    s = Story(
        title=title,
        source_name="Test",
        source_url="https://test.com",
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
# 1–3: Title-first strategy
# ---------------------------------------------------------------------------

class TestTitleFirstStrategy:
    def test_title_matched_tag_before_summary_only(self):
        """A tag matched in the title appears before tags only in the summary."""
        story = _story(
            title="Trump Signs Executive Order on Immigration",
            summary="The order affects millions. Meanwhile France announced new trade deals. Federal Reserve held rates.",
        )
        tags = _generate_hashtags(story)
        assert "#Trump" in tags
        trump_idx = tags.index("#Trump")
        # France should either not appear or appear after Trump
        if "#France" in tags:
            assert tags.index("#France") > trump_idx

    def test_category_tag_before_summary_only(self):
        """Category tags fill slots before summary-only keyword matches."""
        story = _story(
            title="For Populist Democrats, a Movement Fueled by Anger at Both Parties",
            summary="Rising prices drove Democratic candidates. Experts believe wildfires in France. Federal Reserve signals pause.",
            category="politics",
        )
        tags = _generate_hashtags(story)
        # Category tags should appear
        category_tags = {"#Politics", "#Government", "#Democrats", "#Elections", "#USA"}
        found_category = [t for t in tags if t in category_tags]
        assert len(found_category) >= 1, f"No category tags in {tags}"

        # France/FederalReserve should be demoted — if present, behind category tags
        for unrelated in ["#France", "#FederalReserve"]:
            if unrelated in tags:
                unrelated_idx = tags.index(unrelated)
                # At least one category tag should be before it
                category_before = any(
                    tags.index(ct) < unrelated_idx
                    for ct in found_category
                )
                assert category_before, (
                    f"{unrelated} appears before all category tags in {tags}"
                )

    def test_summary_only_tag_is_last_resort(self):
        """Summary-only tags only appear when title+category can't reach min 3."""
        story = _story(
            title="New Climate Agreement Signed at Global Summit",
            summary="Leaders agreed to cut emissions. France participated. Federal Reserve unrelated mention.",
            category="climate",
        )
        tags = _generate_hashtags(story)
        assert len(tags) >= 3
        # Climate category tags should be there
        climate_tags = {"#ClimateChange", "#Climate", "#Environment", "#COP", "#GlobalWarming"}
        found = [t for t in tags if t in climate_tags]
        # If we have 3+ without France/FederalReserve, they shouldn't appear
        non_bleed = [t for t in tags if t not in {"#France", "#FederalReserve"}]
        if len(non_bleed) >= 3:
            assert "#France" not in tags or tags.index("#France") >= 3
            assert "#FederalReserve" not in tags or tags.index("#FederalReserve") >= 3


# ---------------------------------------------------------------------------
# 4–8: Real story scenarios from production failures
# ---------------------------------------------------------------------------

class TestProductionStories:
    def test_democrats_story_no_france_in_first_3(self):
        """The Democrats bleed-over bug: #France must not be in first 3 positions."""
        story = _story(
            title="For Populist Democrats, a Movement Fueled by Anger at Both Parties",
            summary=(
                "A generation shaped by the pandemic and rising prices has helped "
                "drive an emergence of Democratic candidates. Experts believe "
                "one-third of wildfires in France are caused by negligence. "
                "Federal Reserve signals pause."
            ),
            category="politics",
        )
        tags = _generate_hashtags(story)
        assert tags[0] == "#GlobalPulseNews"
        # France and FederalReserve should not crowd out category/title tags
        first_3_excluding_brand = tags[1:4]
        assert "#France" not in first_3_excluding_brand, (
            f"#France should not be in first 3 tags, got: {tags}"
        )

    def test_silicon_valley_no_invented_compounds(self):
        """Silicon Valley story must not produce #USDefense or #WarInIran."""
        story = _story(
            title="How Will Silicon Valley Rearm America?",
            summary=(
                "War in Ukraine, conflict in the Middle East and the rise of China "
                "have pushed national security back to center. Former Defense "
                "Secretary Mark Esper says the US needs Silicon Valley."
            ),
            category="technology",
        )
        tags = _generate_hashtags(story)
        assert "#USDefense" not in tags, f"Invented compound #USDefense in {tags}"
        assert "#WarInIran" not in tags, f"Invented compound #WarInIran in {tags}"
        assert "#Technology" in tags or "#Tech" in tags, f"No tech tag in {tags}"

    def test_jeremy_thomas_entertainment_before_china(self):
        """Filmmaker obituary: #Entertainment should come before #China/#Japan."""
        story = _story(
            title="Jeremy Thomas Remembered as a Filmmaker Full of Stories and a Rebel",
            summary=(
                "Jeremy Thomas, the legendary British producer, passed away Friday "
                "at 77. His films included The Last Emperor set in China and Japan."
            ),
            category="entertainment",
        )
        tags = _generate_hashtags(story)
        assert tags[0] == "#GlobalPulseNews"
        if "#Entertainment" in tags and "#China" in tags:
            assert tags.index("#Entertainment") < tags.index("#China"), (
                f"#Entertainment should come before #China in {tags}"
            )

    def test_trump_zelenskyy_title_tags_dominate(self):
        """Trump/Zelenskyy story: title-matched tags should be in results."""
        story = _story(
            title="Trump Urges Zelenskyy to Stop Hitting Russia Diesel Refineries",
            summary="Trump urged Zelenskyy to stop drone strikes on Russian oil. Putin warned of escalation.",
        )
        tags = _generate_hashtags(story)
        assert "#Trump" in tags or "#Russia" in tags or "#Ukraine" in tags, (
            f"Expected title-relevant tags in {tags}"
        )

    def test_yemen_houthis_title_tags(self):
        """Yemen story: Yemen and Iran should appear (both in title)."""
        story = _story(
            title="Yemen Houthis Test Iran Regional Influence",
            summary="The Houthis in Yemen are testing Iran regional influence amid Saudi Arabia pressure.",
            category="world",
        )
        tags = _generate_hashtags(story)
        assert "#Yemen" in tags or "#Iran" in tags, (
            f"Expected #Yemen or #Iran in {tags}"
        )


# ---------------------------------------------------------------------------
# 9–10: WHO case-sensitivity (regression)
# ---------------------------------------------------------------------------

class TestWHOCaseSensitivity:
    def test_who_uppercase_matched(self):
        story = _story(
            title="WHO Declares Mpox Outbreak a Global Emergency",
            summary="WHO declared the mpox outbreak a global health emergency.",
            category="health",
        )
        tags = _generate_hashtags(story)
        assert "#WHO" in tags, f"Expected #WHO in {tags}"

    def test_who_pronoun_not_matched(self):
        story = _story(
            title="Officials Who Warned About Flooding Were Ignored",
            summary="The officials who issued warnings were not heeded by authorities.",
        )
        tags = _generate_hashtags(story)
        assert "#WHO" not in tags, f"Pronoun 'who' should not produce #WHO, got {tags}"


# ---------------------------------------------------------------------------
# 11–15: Compound tag filtering
# ---------------------------------------------------------------------------

class TestCompoundTagFiltering:
    def test_us_defense_blocked_by_llm_filter(self):
        raw = _raw_with_hashtags("#USDefense #Technology #USA #Military")
        result = _parse_llm_hashtags(raw)
        assert "#USDefense" not in result, f"#USDefense should be blocked, got {result}"

    def test_war_in_iran_blocked_by_llm_filter(self):
        raw = _raw_with_hashtags("#WarInIran #Iran #MiddleEast #Conflict")
        result = _parse_llm_hashtags(raw)
        assert "#WarInIran" not in result, f"#WarInIran should be blocked, got {result}"

    def test_brics_not_blocked(self):
        raw = _raw_with_hashtags("#BRICS #Russia #India #China")
        result = _parse_llm_hashtags(raw)
        assert "#BRICS" in result, f"#BRICS should NOT be blocked, got {result}"

    def test_nato_not_blocked(self):
        raw = _raw_with_hashtags("#NATO #Ukraine #Russia #War")
        result = _parse_llm_hashtags(raw)
        assert "#NATO" in result, f"#NATO should NOT be blocked, got {result}"

    def test_trump_not_blocked(self):
        raw = _raw_with_hashtags("#Trump #USA #Politics #Elections")
        result = _parse_llm_hashtags(raw)
        assert "#Trump" in result, f"#Trump should NOT be blocked, got {result}"

    def test_known_twoword_saudiarabia_not_blocked(self):
        raw = _raw_with_hashtags("#SaudiArabia #Yemen #Iran #MiddleEast")
        result = _parse_llm_hashtags(raw)
        assert "#SaudiArabia" in result, f"#SaudiArabia should NOT be blocked, got {result}"

    def test_known_twoword_middleeast_not_blocked(self):
        raw = _raw_with_hashtags("#MiddleEast #Israel #Gaza #Palestine")
        result = _parse_llm_hashtags(raw)
        assert "#MiddleEast" in result, f"#MiddleEast should NOT be blocked, got {result}"

    def test_known_twoword_celinedion_not_blocked(self):
        raw = _raw_with_hashtags("#CelineDion #France #Music #Entertainment")
        result = _parse_llm_hashtags(raw)
        assert "#CelineDion" in result, f"#CelineDion should NOT be blocked, got {result}"


# ---------------------------------------------------------------------------
# 16–18: Invariants
# ---------------------------------------------------------------------------

class TestHashtagInvariants:
    def test_always_starts_with_globalpulsenews(self):
        for title, summary, cat in [
            ("Turkey earthquake kills 12", "A 6.2 quake struck Turkey.", "breaking"),
            ("Climate summit in Paris", "Leaders agreed on new targets.", "climate"),
            ("Tech earnings disappoint", "Apple and Google missed targets.", "technology"),
        ]:
            story = _story(title, summary, cat)
            tags = _generate_hashtags(story)
            assert tags[0] == "#GlobalPulseNews", f"First tag must be #GlobalPulseNews, got {tags}"

    def test_never_exceeds_5_tags(self):
        story = _story(
            "Trump Ukraine Russia China Iran Israel Pakistan India Summit",
            "Trump met with leaders from Russia, China, Iran, Israel, Pakistan, India, France, Germany.",
        )
        tags = _generate_hashtags(story)
        assert len(tags) <= 5, f"Should never exceed 5 tags, got {len(tags)}: {tags}"

    def test_always_at_least_3_tags(self):
        story = _story(
            "Local council approves new parking rules",
            "A local council approved new parking regulations for the area.",
            "world",
        )
        tags = _generate_hashtags(story)
        assert len(tags) >= 3, f"Should always have at least 3 tags, got {len(tags)}: {tags}"
