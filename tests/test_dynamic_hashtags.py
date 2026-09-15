"""
tests/test_dynamic_hashtags.py — Tests for the dynamic LLM hashtag system.

Covers every path hashtags can take through the pipeline:

PATH A — Full LLM generation, LLM gives good hashtags in post response
PATH B — Full LLM generation, LLM gives bad/missing hashtags → focused call
PATH C — Full LLM generation, focused call also fails → deterministic fallback
PATH D — Grounded fallback path, LLM available → focused call
PATH E — Grounded fallback path, LLM unavailable → deterministic fallback
PATH F — LLM completely down (_GENERATION_BACKEND_UNAVAILABLE set) → deterministic

_build_hashtags:
  - Always starts with #GlobalPulseNews
  - LLM tags used when 2+ valid
  - Deterministic used when < 2 valid
  - #GlobalPulseNews never duplicated from LLM output
  - Returns 3-5 tags always

_fetch_llm_hashtags:
  - Returns [] immediately if backend unavailable
  - Returns [] on any exception (never raises)
  - Returns [] if LLM returns < 2 valid tags
  - Returns validated tags on success
  - Uses story title + category + first summary sentence

Integration:
  - Full generate_post uses LLM tags from post body when valid
  - Full generate_post uses focused call when post body tags invalid
  - Grounded fallback uses focused call
  - All paths produce unique story-specific tags
  - Facebook policy: no duplicate static tags across different stories
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
    _build_hashtags,
    _fetch_llm_hashtags,
    _generate_hashtags,
    _parse_llm_hashtags,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(
    title: str = "Turkey Earthquake Kills 12 People in Malatya Province",
    summary: str = "A 6.2 magnitude earthquake struck eastern Turkey on Tuesday, killing 12 people.",
    category: str = "breaking",
) -> Story:
    s = Story(
        title=title,
        source_name="BBC",
        source_url="https://bbc.com",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        category=category,
    )
    s.verification_status = VerificationStatus.VERIFIED
    s.corroborating_sources = []
    return s


def _mock_llm_response(text: str) -> MagicMock:
    mock = MagicMock()
    mock.choices[0].message.content = text
    return mock


def _make_full_llm_response(hashtag_line: str, post: str = "Post body.") -> str:
    return (
        "CARD_HEADLINES:\n"
        "1. Earthquake Kills Twelve in Turkey\n"
        "2. Turkey Quake Leaves Twelve Dead\n"
        "3. Powerful Quake Strikes Eastern Turkey\n\n"
        "CARD_DESCRIPTION:\nTwelve people died in Malatya province.\n\n"
        f"HASHTAGS:\n{hashtag_line}\n\n"
        f"POST:\n{post}"
    )


# ---------------------------------------------------------------------------
# _build_hashtags
# ---------------------------------------------------------------------------

class TestBuildHashtags:
    def test_llm_tags_used_when_2_or_more(self):
        story = _story()
        result = _build_hashtags(story, ["#Earthquake", "#Turkey"])
        assert result[0] == "#GlobalPulseNews"
        assert "#Earthquake" in result
        assert "#Turkey" in result

    def test_llm_tags_used_when_4_given(self):
        story = _story()
        result = _build_hashtags(story, ["#Earthquake", "#Turkey", "#Malatya", "#NaturalDisaster"])
        assert "#GlobalPulseNews" in result
        assert len(result) <= 5

    def test_deterministic_fallback_when_1_llm_tag(self):
        story = _story()
        result = _build_hashtags(story, ["#Earthquake"])
        assert result[0] == "#GlobalPulseNews"
        assert len(result) >= 3

    def test_deterministic_fallback_when_0_llm_tags(self):
        story = _story()
        result = _build_hashtags(story, [])
        assert result[0] == "#GlobalPulseNews"
        assert len(result) >= 3

    def test_globalpulsenews_never_duplicated(self):
        story = _story()
        result = _build_hashtags(story, ["#GlobalPulseNews", "#Earthquake", "#Turkey"])
        assert result.count("#GlobalPulseNews") == 1

    def test_always_starts_with_globalpulsenews(self):
        story = _story()
        for llm_tags in [[], ["#A"], ["#Earthquake", "#Turkey"], ["#A", "#B", "#C", "#D"]]:
            result = _build_hashtags(story, llm_tags)
            assert result[0] == "#GlobalPulseNews", f"Failed with llm_tags={llm_tags}"

    def test_always_at_least_3_tags(self):
        story = _story()
        for llm_tags in [[], ["#A"], ["#B", "#C"]]:
            result = _build_hashtags(story, llm_tags)
            assert len(result) >= 3, f"Too few tags: {result}"

    def test_never_more_than_5_tags(self):
        story = _story()
        result = _build_hashtags(story, ["#A", "#B", "#C", "#D", "#E", "#F"])
        assert len(result) <= 5

    def test_returns_list_always(self):
        story = _story()
        assert isinstance(_build_hashtags(story, []), list)
        assert isinstance(_build_hashtags(story, ["#Gaza", "#Israel"]), list)


# ---------------------------------------------------------------------------
# _fetch_llm_hashtags
# ---------------------------------------------------------------------------

class TestFetchLlmHashtags:
    def test_returns_empty_when_backend_unavailable(self):
        with patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", "Ollama offline"):
            result = _fetch_llm_hashtags(_story())
        assert result == []

    def test_returns_empty_on_exception(self):
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.side_effect = Exception("connection refused")
            result = _fetch_llm_hashtags(_story())
        assert result == []

    def test_never_raises_on_any_error(self):
        errors = [
            Exception("network error"),
            ConnectionError("refused"),
            TimeoutError("timed out"),
            ValueError("unexpected"),
            RuntimeError("crash"),
        ]
        for err in errors:
            with patch("pipeline.generator._get_client") as mock_client, \
                 patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
                mock_client.side_effect = err
                try:
                    result = _fetch_llm_hashtags(_story())
                    assert isinstance(result, list)
                except Exception as e:
                    pytest.fail(f"_fetch_llm_hashtags raised {type(e).__name__}: {e}")

    def test_returns_empty_when_llm_gives_1_valid_tag(self):
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response("#Earthquake")
            result = _fetch_llm_hashtags(_story())
        assert result == []

    def test_returns_empty_when_llm_gives_only_blocked_tags(self):
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response("#News #Update #Breaking #Latest")
            result = _fetch_llm_hashtags(_story())
        assert result == []

    def test_returns_valid_tags_on_success(self):
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response("#Earthquake #Turkey #Malatya #NaturalDisaster")
            result = _fetch_llm_hashtags(_story())
        assert len(result) >= 2
        assert "#Earthquake" in result
        assert "#Turkey" in result

    def test_returns_empty_when_llm_returns_empty_string(self):
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response("")
            result = _fetch_llm_hashtags(_story())
        assert result == []

    def test_uses_story_title_in_prompt(self):
        captured = {}
        def fake_create(**kwargs):
            captured["messages"] = kwargs.get("messages", [])
            return _mock_llm_response("#Gaza #Israel #MiddleEast #Ceasefire")

        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.side_effect = fake_create
            story = _story(title="Gaza Ceasefire Talks Collapse in Cairo")
            _fetch_llm_hashtags(story)

        user_msg = captured["messages"][1]["content"]
        assert "Gaza Ceasefire Talks Collapse in Cairo" in user_msg

    def test_uses_story_category_in_prompt(self):
        captured = {}
        def fake_create(**kwargs):
            captured["messages"] = kwargs.get("messages", [])
            return _mock_llm_response("#Gaza #Israel #MiddleEast #Ceasefire")

        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.side_effect = fake_create
            story = _story(category="politics")
            _fetch_llm_hashtags(story)

        user_msg = captured["messages"][1]["content"]
        assert "politics" in user_msg.lower()

    def test_uses_low_temperature(self):
        captured = {}
        def fake_create(**kwargs):
            captured["kwargs"] = kwargs
            return _mock_llm_response("#Gaza #Israel #MiddleEast #Ceasefire")

        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.side_effect = fake_create
            _fetch_llm_hashtags(_story())

        temp = captured["kwargs"].get("temperature", 1.0)
        assert temp <= 0.6, f"Temperature should be <= 0.6, got {temp}"


# ---------------------------------------------------------------------------
# PATH A — Full LLM success, valid hashtags in post response
# ---------------------------------------------------------------------------

class TestPathA_LLMSuccessValidTags:
    def test_llm_post_tags_used_directly(self):
        from pipeline.generator import generate_post
        story = _story()
        llm_response = _make_full_llm_response(
            "#Earthquake #Turkey #Malatya #NaturalDisaster",
            "🔴 BREAKING: 12 dead in Turkey earthquake.\n\nWhy this matters: rescue underway.\n\nSources: BBC"
        )
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response(llm_response)
            result = generate_post(story)
        assert result.hashtags[0] == "#GlobalPulseNews"
        assert "#Earthquake" in result.hashtags or "#Turkey" in result.hashtags

    def test_globalpulsenews_always_first_on_path_a(self):
        from pipeline.generator import generate_post
        story = _story()
        llm_response = _make_full_llm_response(
            "#Earthquake #Turkey #Malatya #NaturalDisaster",
            "🔴 BREAKING: 12 dead.\n\nWhy this matters: rescue underway.\n\nSources: BBC"
        )
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response(llm_response)
            result = generate_post(story)
        assert result.hashtags[0] == "#GlobalPulseNews"


# ---------------------------------------------------------------------------
# PATH B — Full LLM success, but post body has bad tags → focused call
# ---------------------------------------------------------------------------

class TestPathB_LLMSuccessBadTagsFocusedCall:
    def test_focused_call_triggered_when_post_tags_invalid(self):
        from pipeline.generator import generate_post
        story = _story()
        llm_response = _make_full_llm_response(
            "#News #Update #Breaking #Latest",
            "🔴 BREAKING: 12 dead in Turkey earthquake.\n\nWhy this matters: rescue.\n\nSources: BBC"
        )
        good_focused = _mock_llm_response("#Earthquake #Turkey #Malatya #Disaster")
        call_count = [0]
        def fake_create(**kwargs):
            call_count[0] += 1
            msgs = kwargs.get("messages", [])
            if any("hashtag specialist" in (m.get("content", "")) for m in msgs):
                return good_focused
            return _mock_llm_response(llm_response)
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.side_effect = fake_create
            result = generate_post(story)
        assert result.hashtags[0] == "#GlobalPulseNews"
        assert len(result.hashtags) >= 3


# ---------------------------------------------------------------------------
# PATH C — Both LLM calls fail → deterministic
# ---------------------------------------------------------------------------

class TestPathC_BothLLMFailsDeterministicFallback:
    def test_deterministic_used_when_both_llm_calls_fail(self):
        from pipeline.generator import generate_post
        story = _story()
        llm_response = _make_full_llm_response(
            "#News #Update",
            "🔴 BREAKING: 12 dead in Turkey earthquake.\n\nWhy this matters: rescue.\n\nSources: BBC"
        )
        def fake_create(**kwargs):
            msgs = kwargs.get("messages", [])
            if any("hashtag specialist" in (m.get("content", "")) for m in msgs):
                return _mock_llm_response("#News #Update")
            return _mock_llm_response(llm_response)
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.side_effect = fake_create
            result = generate_post(story)
        assert result.hashtags[0] == "#GlobalPulseNews"
        assert len(result.hashtags) >= 3


# ---------------------------------------------------------------------------
# PATH D — Grounded fallback, LLM available → focused call
# ---------------------------------------------------------------------------

class TestPathD_GroundedFallbackLLMAvailable:
    def test_grounded_fallback_uses_focused_llm_call(self):
        from pipeline.generator import _apply_grounded_fallback
        story = _story()
        with patch("pipeline.generator._fetch_llm_hashtags",
                   return_value=["#Earthquake", "#Turkey", "#Malatya"]) as mock_fetch, \
             patch("pipeline.generator._fallback_card_headline", return_value="Headline"), \
             patch("pipeline.generator._grounded_fallback_post",
                   return_value="🔴 BREAKING: 12 dead.\n\nWhy this matters: rescue.\n\nSources: BBC"), \
             patch("pipeline.post_fact_checker.check_generated_facts") as mock_check:
            mock_check.return_value = MagicMock(passed=True)
            result = _apply_grounded_fallback(story)
        mock_fetch.assert_called_once_with(story)
        assert result.hashtags[0] == "#GlobalPulseNews"
        assert "#Earthquake" in result.hashtags or "#Turkey" in result.hashtags

    def test_different_grounded_stories_get_different_tags(self):
        from pipeline.generator import _apply_grounded_fallback
        results = []
        for title, tags in [
            ("Gaza Ceasefire Talks Collapse", ["#Gaza", "#Israel", "#MiddleEast", "#Ceasefire"]),
            ("Jeremy Thomas Filmmaker Dies", ["#JeremyThomas", "#BritishFilm", "#Cinema", "#FilmProducer"]),
        ]:
            story = _story(title=title)
            with patch("pipeline.generator._fetch_llm_hashtags", return_value=tags), \
                 patch("pipeline.generator._fallback_card_headline", return_value="Headline"), \
                 patch("pipeline.generator._grounded_fallback_post",
                       return_value="🔴 BREAKING: Event.\n\nWhy this matters: impact.\n\nSources: BBC"), \
                 patch("pipeline.post_fact_checker.check_generated_facts") as mock_check:
                mock_check.return_value = MagicMock(passed=True)
                result = _apply_grounded_fallback(story)
            results.append(set(result.hashtags))
        assert results[0] != results[1], "Different stories must have different hashtags"


# ---------------------------------------------------------------------------
# PATH E — Grounded fallback, LLM unavailable → deterministic
# ---------------------------------------------------------------------------

class TestPathE_GroundedFallbackLLMDown:
    def test_grounded_fallback_deterministic_when_llm_down(self):
        import pipeline.generator as gen_mod
        from pipeline.generator import _apply_grounded_fallback
        story = _story()
        original = gen_mod._GENERATION_BACKEND_UNAVAILABLE
        gen_mod._GENERATION_BACKEND_UNAVAILABLE = "Ollama offline"
        try:
            with patch("pipeline.generator._fallback_card_headline", return_value="Headline"), \
                 patch("pipeline.generator._grounded_fallback_post",
                       return_value="🔴 BREAKING: 12 dead.\n\nWhy this matters: rescue.\n\nSources: BBC"), \
                 patch("pipeline.post_fact_checker.check_generated_facts") as mock_check:
                mock_check.return_value = MagicMock(passed=True)
                result = _apply_grounded_fallback(story)
        finally:
            gen_mod._GENERATION_BACKEND_UNAVAILABLE = original
        assert result.hashtags[0] == "#GlobalPulseNews"
        assert len(result.hashtags) >= 3


# ---------------------------------------------------------------------------
# PATH F — _GENERATION_BACKEND_UNAVAILABLE at generate_post entry
# ---------------------------------------------------------------------------

class TestPathF_BackendUnavailableAtEntry:
    def test_generate_post_uses_grounded_fallback_when_backend_down(self):
        from pipeline.generator import generate_post
        story = _story()
        with patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", "Ollama connection refused"), \
             patch("pipeline.generator._enrich_story_context", return_value=story), \
             patch("pipeline.generator._apply_grounded_fallback") as mock_fallback:
            mock_story = _story()
            mock_story.hashtags = ["#GlobalPulseNews", "#Earthquake", "#Turkey"]
            mock_story.post_content = "Post content."
            mock_story.card_headline = "Headline"
            mock_fallback.return_value = mock_story
            result = generate_post(story)
        mock_fallback.assert_called_once()
        assert result.hashtags[0] == "#GlobalPulseNews"


# ---------------------------------------------------------------------------
# Facebook policy: no repetitive static tags across different stories
# ---------------------------------------------------------------------------

class TestFacebookPolicyUniqueness:
    def test_different_stories_get_different_hashtags(self):
        stories_and_tags = [
            ("Gaza Ceasefire Talks Collapse in Cairo Negotiations",
             ["#Gaza", "#Israel", "#MiddleEast", "#Ceasefire"]),
            ("Jeremy Thomas Filmmaker Remembered as a True Rebel",
             ["#JeremyThomas", "#BritishFilm", "#Cinema", "#FilmProducer"]),
            ("Turkey Earthquake Kills Twelve in Eastern Malatya Province",
             ["#Earthquake", "#Turkey", "#Malatya", "#NaturalDisaster"]),
        ]
        all_hashtag_sets = []
        for title, llm_tags in stories_and_tags:
            story = _story(title=title)
            result = _build_hashtags(story, llm_tags)
            story_tags = set(t for t in result if t != "#GlobalPulseNews")
            all_hashtag_sets.append(story_tags)
        for i in range(len(all_hashtag_sets)):
            for j in range(i + 1, len(all_hashtag_sets)):
                overlap = all_hashtag_sets[i] & all_hashtag_sets[j]
                assert len(overlap) <= 1, (
                    f"Stories {i} and {j} share too many tags: {overlap}"
                )

    def test_llm_tags_are_story_specific_not_generic(self):
        story = _story(title="Gaza Ceasefire Talks Collapse")
        llm_tags = ["#Gaza", "#Israel", "#MiddleEast", "#Ceasefire"]
        result = _build_hashtags(story, llm_tags)
        generic = {"#News", "#Update", "#Latest", "#Breaking", "#Trending", "#WorldNews"}
        overlap = set(result) & generic
        assert len(overlap) == 0, f"Generic tags found in result: {overlap}"

    def test_fetch_llm_hashtags_never_returns_globalpulsenews(self):
        """#GlobalPulseNews must never come from the focused LLM call."""
        with patch("pipeline.generator._get_client") as mock_client, \
             patch("pipeline.generator._GENERATION_BACKEND_UNAVAILABLE", None):
            mock_client.return_value.chat.completions.create.return_value = \
                _mock_llm_response("#GlobalPulseNews #Gaza #Israel #MiddleEast")
            result = _fetch_llm_hashtags(_story())
        assert "#GlobalPulseNews" not in result
        assert "#globalpulsenews" not in [t.lower() for t in result]
