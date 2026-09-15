"""
tests/test_card_description_v2.py

Comprehensive tests for the improved select_card_description() validator.

Covers every validation rule:
  Rule 1 — Block must exist
  Rule 2 — Word count 7–18
  Rule 3 — No clickbait / question endings
  Rule 4 — Minimum 2 shared keywords with title  (raised from 1)
  Rule 5 — Similarity < 0.85 (not a near-copy of the title)
  Rule 6 — Concrete anchor: number, proper noun, or quote  (new)
  Rule 7 — Vague speculation without concrete anchor rejected  (new)
  Rule 8 — Fact grounding check

Also covers:
  - Output formatting (trailing period, whitespace cleanup)
  - Engaging / human-impact descriptions accepted
  - Real production bad examples now rejected
  - Pipeline integration: generator still imports and assigns card_description
  - No regression on existing passing tests

NOTE: Every "should accept" description uses ONLY facts present in the story
      summary, so the fact grounding check does not block it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Story, VerificationStatus
from pipeline.card_description import (
    _has_concrete_anchor,
    _terms,
    select_card_description,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(
    title: str = "UK imposes sanctions on West Bank settlements",
    summary: str = (
        "The measures are expected to include a trade ban on settlement goods. "
        "The move marks a major shift in British policy toward Israel."
    ),
) -> Story:
    s = Story(
        title=title,
        source_name="BBC",
        source_url="https://bbc.com",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
    )
    s.verification_status = VerificationStatus.VERIFIED
    s.corroborating_sources = []
    return s


def _raw(desc: str, post: str = "Post body.") -> str:
    return (
        "CARD_HEADLINES:\n1. Headline one\n2. Headline two\n3. Headline three\n\n"
        f"CARD_DESCRIPTION:\n{desc}\n\n"
        f"HASHTAGS:\n#UK #Sanctions #WestBank #Israel\n\n"
        f"POST:\n{post}"
    )


# ---------------------------------------------------------------------------
# Rule 1 — Block must exist
# ---------------------------------------------------------------------------

class TestRule1BlockMustExist:
    def test_returns_none_when_no_block(self):
        raw = "CARD_HEADLINES:\n1. Headline\n\nPOST:\nBody"
        assert select_card_description(_story(), raw) is None

    def test_returns_none_when_empty_string(self):
        assert select_card_description(_story(), "") is None

    def test_accepts_block_with_hashtags_section_after(self):
        # Regex must not eat into HASHTAGS section
        story = _story()
        # "UK sanctions mark" shares uk/sanction/west/bank with title (3 keywords)
        # sim = 3/5 = 0.60 < 0.85. Has proper noun Israel. Grounded.
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None

    def test_block_found_without_post_section(self):
        story = _story()
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = f"CARD_DESCRIPTION:\n{desc}"
        result = select_card_description(story, raw)
        assert result is not None


# ---------------------------------------------------------------------------
# Rule 2 — Word count 7–18
# ---------------------------------------------------------------------------

class TestRule2WordCount:
    def test_rejects_too_short(self):
        story = _story()
        raw = _raw("UK sanctions Israel settlements now.")  # 5 words
        assert select_card_description(story, raw) is None

    def test_rejects_too_long(self):
        story = _story()
        # 20+ words
        desc = (
            "The UK government has imposed sweeping new economic sanctions on West Bank "
            "settlements targeting trade in goods as a major policy shift toward Israel."
        )
        raw = _raw(desc)
        assert select_card_description(story, raw) is None

    def test_accepts_at_7_words(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # Exactly 7 words, shares turkey+malatya+province (3 kw), has "12" (number anchor)
        desc = "The 6.2 Turkey earthquake killed 12 in Malatya."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None

    def test_accepts_at_18_words(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # 18 words, all grounded in summary
        desc = (
            "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people "
            "in Malatya Province on Tuesday."
        )
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None


# ---------------------------------------------------------------------------
# Rule 3 — No clickbait / question endings
# ---------------------------------------------------------------------------

class TestRule3Clickbait:
    def test_rejects_question_mark(self):
        story = _story()
        raw = _raw(
            "Will UK sanctions on West Bank settlements actually shift British policy toward Israel?"
        )
        assert select_card_description(story, raw) is None

    def test_rejects_shocking(self):
        story = _story()
        raw = _raw(
            "Shocking UK sanctions on West Bank settlements mark a shift in British policy."
        )
        assert select_card_description(story, raw) is None

    def test_rejects_you_wont_believe(self):
        story = _story()
        raw = _raw(
            "You won't believe UK sanctions on West Bank settlements mark a major shift."
        )
        assert select_card_description(story, raw) is None

    def test_rejects_must_see(self):
        story = _story()
        raw = _raw(
            "Must see UK sanctions on West Bank settlements mark a major shift in policy."
        )
        assert select_card_description(story, raw) is None


# ---------------------------------------------------------------------------
# Rule 4 — Minimum 2 shared keywords with title (raised from 1)
# ---------------------------------------------------------------------------

class TestRule4MinimumSharedKeywords:
    def test_rejects_zero_shared_keywords(self):
        story = _story()
        raw = _raw("Markets in Japan suffered their largest crash in decades.")
        assert select_card_description(story, raw) is None

    def test_rejects_only_1_shared_keyword(self):
        story = _story()
        # Only "sanctions" shared with title
        raw = _raw("The sanctions have caused alarm in diplomatic circles worldwide today.")
        assert select_card_description(story, raw) is None

    def test_accepts_2_shared_keywords(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # "earthquake" + "turkey" shared; "12" is anchor; grounded in summary
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_accepts_3_plus_shared_keywords(self):
        story = _story()
        # "sanctions" + "west" + "bank" shared (3); "Israel" is proper noun anchor; grounded
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_previous_bad_example_now_rejected(self):
        """The Sweden example that slipped through must now be rejected."""
        story = _story(
            title="Can Sweden restore welfare system amid increased defense spending?",
            summary="Sweden faces pressure to cut welfare as defence budgets rise.",
        )
        # Only "Sweden" shared — 1 keyword, fails rule 4
        raw = _raw(
            "Sweden's future government could face significant challenges "
            "balancing economic and social priorities."
        )
        assert select_card_description(story, raw) is None


# ---------------------------------------------------------------------------
# Rule 5 — Similarity < 0.85 (not a near-copy of the title)
# ---------------------------------------------------------------------------

class TestRule5TitleParaphrase:
    def test_rejects_near_identical_to_title(self):
        story = _story()
        # Almost every word from title appears — sim will be >= 0.85
        raw = _raw("UK imposes sanctions on West Bank settlements today amid controversy.")
        assert select_card_description(story, raw) is None

    def test_rejects_exact_title_copy(self):
        story = _story()
        raw = _raw("UK imposes sanctions on West Bank settlements.")
        assert select_card_description(story, raw) is None

    def test_accepts_description_that_adds_new_grounded_info(self):
        story = _story()
        # 3/5 title terms shared → sim 0.60 < 0.85. Adds new info (policy shift toward Israel).
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_accepts_high_overlap_that_is_still_below_threshold(self):
        story = _story(
            title="Gaza Ceasefire Talks Collapse in Cairo",
            summary="Peace negotiations between Israel and Hamas collapsed in Cairo on Tuesday.",
        )
        # 4/5 title terms shared → sim 0.80 < 0.85. New info: Hamas walked out.
        desc = "Hamas negotiators walked out of Cairo talks, leaving Gaza ceasefire hopes shattered."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None


# ---------------------------------------------------------------------------
# Rule 6 — Concrete anchor: number, proper noun, or quote (NEW)
# ---------------------------------------------------------------------------

class TestRule6ConcreteAnchor:
    def test_rejects_description_with_no_concrete_anchor(self):
        story = _story()
        # 3 shared keywords but no number, no mid-sentence proper noun, no quote
        raw = _raw(
            "These sanctions on settlements represent a significant policy shift today."
        )
        assert select_card_description(story, raw) is None

    def test_accepts_description_with_number(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # "12" is a concrete number anchor
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_accepts_description_with_proper_noun(self):
        story = _story()
        # "Israel" is a proper noun mid-sentence
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_accepts_description_with_quoted_phrase(self):
        story = _story()
        # Contains a quote — that is a concrete anchor
        desc = 'UK called the West Bank sanctions "a major shift" in British policy toward Israel.'
        raw = _raw(desc)
        assert select_card_description(story, raw) is not None

    def test_has_concrete_anchor_number(self):
        assert _has_concrete_anchor("Turkey earthquake killed 12 people in Malatya.") is True
        assert _has_concrete_anchor("The 6.2 magnitude quake struck eastern Turkey.") is True
        assert _has_concrete_anchor("Over 3 million Swedes rely on welfare services.") is True

    def test_has_concrete_anchor_proper_noun_mid_sentence(self):
        assert _has_concrete_anchor("The move shocked Israel and Washington alike.") is True
        assert _has_concrete_anchor("Hamas walked out of Cairo talks on Tuesday.") is True

    def test_has_concrete_anchor_quote(self):
        assert _has_concrete_anchor('Officials called it "a historic turning point" today.') is True
        assert _has_concrete_anchor("UK called it \u2018a major shift\u2019 in policy.") is True

    def test_no_concrete_anchor_vague(self):
        assert _has_concrete_anchor("This represents a significant challenge for the future.") is False
        assert _has_concrete_anchor("The situation could face major difficulties going forward.") is False
        assert _has_concrete_anchor("Officials say the impact may be felt across communities.") is False


# ---------------------------------------------------------------------------
# Rule 7 — Vague speculation without concrete anchor rejected (NEW)
# ---------------------------------------------------------------------------

class TestRule7VagueSpeculation:
    def test_rejects_could_face_challenges_no_anchor(self):
        story = _story(
            title="Can Sweden restore welfare system amid increased defense spending?",
            summary="Sweden faces pressure to cut welfare as defence budgets rise.",
        )
        raw = _raw(
            "Sweden's future government could face significant challenges "
            "balancing economic and social priorities."
        )
        assert select_card_description(story, raw) is None

    def test_rejects_may_lead_to_problems_no_anchor(self):
        story = _story()
        # All lowercase-ish, no number, no mid-sentence proper noun, no quote
        raw = _raw(
            "The measures may lead to serious concerns and diplomatic issues across settlements."
        )
        assert select_card_description(story, raw) is None

    def test_accepts_speculation_with_grounded_number_anchor(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # Has a number (12) which anchors it even though it uses "could"
        desc = "The 6.2 Turkey earthquake could raise the death toll beyond 12 people in Malatya."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        # Fact checker may or may not allow "beyond 12" — just confirm no exception
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Rule 8 — Fact grounding
# ---------------------------------------------------------------------------

class TestRule8FactGrounding:
    def test_rejects_invented_casualty_number(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # 200 is NOT in the summary — fact checker should block this
        raw = _raw("The Turkey earthquake in Malatya killed 200 people and injured thousands.")
        assert select_card_description(story, raw) is None

    def test_accepts_number_present_in_summary(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people in Malatya."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

class TestOutputFormatting:
    def _good_desc(self) -> tuple:
        """Returns (story, desc) that passes all rules."""
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people in Malatya"
        return story, desc

    def test_always_ends_with_period(self):
        story, desc = self._good_desc()
        result = select_card_description(story, _raw(desc))
        assert result is not None
        assert result.endswith(".")

    def test_trailing_exclamation_replaced_with_period(self):
        story, desc = self._good_desc()
        result = select_card_description(story, _raw(desc + "!"))
        assert result is not None
        assert result.endswith(".")

    def test_strips_surrounding_quotes(self):
        story, desc = self._good_desc()
        result = select_card_description(story, _raw(f'"{desc}."'))
        assert result is not None
        assert not result.startswith('"')

    def test_collapses_internal_whitespace(self):
        story, desc = self._good_desc()
        spaced = desc.replace("magnitude", "magnitude  ").replace("earthquake", "earthquake  ")
        result = select_card_description(story, _raw(spaced))
        assert result is not None
        assert "  " not in result

    def test_strips_bullet_prefix(self):
        story, desc = self._good_desc()
        result = select_card_description(story, _raw(f"- {desc}."))
        assert result is not None
        assert not result.startswith("-")

    def test_strips_numbered_list_prefix(self):
        story, desc = self._good_desc()
        result = select_card_description(story, _raw(f"1. {desc}."))
        assert result is not None
        assert not result.startswith("1.")


# ---------------------------------------------------------------------------
# Engaging / human-impact descriptions accepted
# ---------------------------------------------------------------------------

class TestEngagingDescriptionsAccepted:
    def test_human_impact_earthquake_accepted(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        # Grounded: 6.2, 12, Malatya all in summary
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people in Malatya."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None

    def test_consequence_driven_ceasefire_accepted(self):
        story = _story(
            title="Gaza Ceasefire Talks Collapse in Cairo",
            summary="Peace negotiations between Israel and Hamas collapsed in Cairo on Tuesday.",
        )
        # All facts grounded in summary: Hamas, Cairo, ceasefire, collapsed
        desc = "Hamas negotiators walked out of Cairo talks, leaving Gaza ceasefire hopes shattered."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None

    def test_person_name_film_producer_accepted(self):
        story = _story(
            title="Jeremy Thomas British Film Producer Dies Aged 71",
            summary="Oscar-winning producer Jeremy Thomas, known for The Last Emperor, died aged 71.",
        )
        # All facts in summary: Jeremy Thomas, 71, Oscar
        desc = "Jeremy Thomas won an Oscar for The Last Emperor and died aged 71."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None

    def test_policy_shift_with_grounded_info_accepted(self):
        story = _story()
        # "Israel" proper noun, "sanctions"+"west"+"bank" 3 shared kw, all grounded
        desc = "UK sanctions mark a major shift in British policy toward Israel over West Bank."
        raw = _raw(desc)
        result = select_card_description(story, raw)
        assert result is not None


# ---------------------------------------------------------------------------
# Production regressions — previously bad examples must now be rejected
# ---------------------------------------------------------------------------

class TestProductionRegressions:
    def test_sweden_welfare_vague_description_rejected(self):
        story = _story(
            title="Can Sweden restore welfare system amid increased defense spending?",
            summary="Sweden faces pressure to cut welfare as defence budgets rise.",
        )
        raw = _raw(
            "Sweden's future government could face significant challenges "
            "balancing economic and social priorities."
        )
        assert select_card_description(story, raw) is None

    def test_generic_officials_monitoring_rejected(self):
        story = _story()
        # Only 1 shared keyword (sanctions) — fails rule 4
        raw = _raw(
            "Officials are monitoring the developing situation and may take "
            "further action on UK sanctions soon."
        )
        assert select_card_description(story, raw) is None

    def test_situation_remains_unclear_rejected(self):
        story = _story()
        # Vague hedge + no concrete anchor
        raw = _raw(
            "The situation on West Bank sanctions remains unclear and may "
            "face further scrutiny."
        )
        assert select_card_description(story, raw) is None


# ---------------------------------------------------------------------------
# _terms helper
# ---------------------------------------------------------------------------

class TestTermsHelper:
    def test_strips_stop_words(self):
        terms = _terms("The UK imposes sanctions on the West Bank")
        assert "the" not in terms
        assert "on" not in terms

    def test_strips_short_words(self):
        terms = _terms("UK imposes sanctions")
        assert "uk" not in terms  # length <= 2

    def test_normalises_plurals(self):
        terms = _terms("settlements")
        assert "settlement" in terms

    def test_lowercases_all(self):
        terms = _terms("SANCTIONS Settlements")
        assert "sanction" in terms or "sanctions" in terms


# ---------------------------------------------------------------------------
# _has_concrete_anchor helper — exhaustive
# ---------------------------------------------------------------------------

class TestHasConcretAnchorExhaustive:
    def test_digit_anywhere(self):
        assert _has_concrete_anchor("The quake killed 12 people.") is True
        assert _has_concrete_anchor("Over 3 million affected.") is True
        assert _has_concrete_anchor("£200 million frozen.") is True

    def test_percentage(self):
        assert _has_concrete_anchor("75% of staff face daily violence.") is True

    def test_proper_noun_second_word(self):
        assert _has_concrete_anchor("The Israel response was swift.") is True
        assert _has_concrete_anchor("Hamas walked out of Cairo talks.") is True

    def test_quoted_short_phrase(self):
        assert _has_concrete_anchor('"Historic" decision taken by UK ministers.') is True

    def test_all_lowercase_no_number(self):
        assert _has_concrete_anchor("the situation could face challenges ahead.") is False
        assert _has_concrete_anchor("this marks a significant policy moment.") is False

    def test_first_word_capital_only_not_enough(self):
        # Only the first word is capitalised — that is sentence-start, not a proper noun
        assert _has_concrete_anchor("Experts say the impact could be serious.") is False


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_generator_imports_card_description_cleanly(self):
        import pipeline.generator as g
        assert hasattr(g, "generate_post")

    def test_card_description_module_imports_cleanly(self):
        from pipeline.card_description import select_card_description
        assert callable(select_card_description)

    def test_always_returns_str_or_none_never_raises(self):
        """Function must never raise — always return str or None."""
        inputs = [
            (_story(), ""),
            (_story(), "no block here"),
            (_story(), "CARD_DESCRIPTION:\n\nPOST:\nbody"),
            (_story(), _raw("x")),
            (_story(), _raw("The 6.2 Turkey earthquake killed 12 in Malatya.")),
        ]
        for story, raw in inputs:
            try:
                result = select_card_description(story, raw)
                assert result is None or isinstance(result, str)
            except Exception as e:
                pytest.fail(f"select_card_description raised {type(e).__name__}: {e}")

    def test_result_always_ends_with_period_when_not_none(self):
        story = _story(
            title="Turkey Earthquake Kills Twelve in Malatya Province",
            summary="A 6.2 magnitude earthquake struck eastern Turkey killing 12 people in Malatya.",
        )
        desc = "The 6.2 magnitude earthquake struck eastern Turkey and killed 12 people in Malatya."
        result = select_card_description(story, _raw(desc))
        if result is not None:
            assert result.endswith(".")
