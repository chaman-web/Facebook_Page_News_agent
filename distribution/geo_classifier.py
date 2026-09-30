"""Conservative geography classification from explicitly verified evidence."""

from __future__ import annotations

import re

from distribution.models import DistributionStory, GeoResult


_PLACES = {
    "US": ("united states", "u.s.", "usa", "america", "hawaii", "california", "texas", "new york", "washington"),
    "PK": ("pakistan", "islamabad", "karachi", "lahore", "balochistan", "punjab", "sindh"),
    "MX": ("mexico", "baja california", "sonora", "acapulco"),
    "AE": ("united arab emirates", "uae", "dubai", "abu dhabi"),
    "CA": ("canada", "canadian", "ottawa", "toronto"),
    "GB": ("united kingdom", "britain", "british", "england", "london"),
    "CN": ("china", "chinese", "beijing"),
    "IN": ("india", "indian", "new delhi", "mumbai"),
}


def classify_geography(story: DistributionStory) -> GeoResult:
    evidence_text = " ".join(story.verified_locations + story.verified_claims).lower()
    if any(value.strip().lower() == "global" for value in story.verified_locations):
        return GeoResult("GLOBAL", None, [], "GLOBAL", 0.95, ["verified location: GLOBAL"])
    matches: list[tuple[str, list[str]]] = []
    for country, terms in _PLACES.items():
        found = [term for term in terms if re.search(rf"\b{re.escape(term)}\b", evidence_text)]
        if found:
            matches.append((country, found))
    if not matches:
        return GeoResult(None, None, [], "UNKNOWN", 0.0, [])

    location_order = [country for item in story.verified_locations if (country := _country_for(item))]
    matches.sort(key=lambda item: (location_order.index(item[0]) if item[0] in location_order else len(location_order), -len(item[1])))
    countries = [item[0] for item in matches]
    primary = countries[0]
    additional = countries[1:]
    region = next((location for location in story.verified_locations if _country_for(location) == primary), None)
    scope = "MULTI_COUNTRY" if additional else "NATIONAL"
    confidence = min(0.99, 0.72 + 0.08 * len(matches[0][1]) + (0.10 if region else 0))
    evidence = [f"{country}: {', '.join(found)}" for country, found in matches]
    return GeoResult(primary, region, additional, scope, round(confidence, 2), evidence)


def _country_for(text: str) -> str | None:
    value = text.lower()
    candidates: list[tuple[int, str]] = []
    for country, terms in _PLACES.items():
        for term in terms:
            if re.search(rf"\b{re.escape(term)}\b", value):
                candidates.append((len(term), country))
    return max(candidates, default=(0, None))[1]


def _location_countries(story: DistributionStory) -> set[str]:
    return {country for item in story.verified_locations if (country := _country_for(item))}
