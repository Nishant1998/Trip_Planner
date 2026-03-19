"""Conservative name matching, not fuzzy geographical inference."""

import re
import unicodedata

from app.planning_tools.contracts import Place


def words(value):
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def resolve_label(value, labels):
    """Accept formatting differences or one unambiguous short city name."""
    if not value:
        return None
    exact = [label for label in labels if words(label) == words(value)]
    if len(exact) == 1:
        return exact[0]
    short = [label for label in labels if words(label.split(",")[0]) == words(value)]
    return short[0] if len(short) == 1 else None


def city_points(entries, labels):
    candidates = {}
    for tool, _, response in entries:
        if tool != "places":
            continue
        values = response.data if isinstance(response.data, list) else [response.data]
        for place in values:
            if (not isinstance(place, Place) or not place.location
                    or not set(place.types).intersection({"locality", "postal_town", "administrative_area_level_3"})):
                continue
            label = resolve_label(place.name, labels)
            if not label:
                continue
            address = words(place.address or "")
            context = [words(part) for part in label.split(",")[1:]]
            if any(not any(address[n:n + len(part)] == part
                           for n in range(len(address) - len(part) + 1)) for part in context):
                continue
            candidates.setdefault(label, {})[place.place_id] = place
    return {label: next(iter(places.values())) for label, places in candidates.items()
            if len(places) == 1}


# Site-specific aliases are documented, never a generic fort/palace substitution.
# https://www.tourism.rajasthan.gov.in/jaipur.html (Amber Fort & Palace Complex)
JAIPUR_AMBER_NAMES = {words(name) for name in
                      ("Amber Fort", "Amer Fort", "Amber Palace", "Amer Palace")}


def matches_required_place(required, name, destination):
    wanted, supplied = words(required), words(name)
    if not wanted:
        return False
    if any(supplied[n:n + len(wanted)] == wanted
           for n in range(len(supplied) - len(wanted) + 1)):
        return True
    return (words(destination.split(",")[0]) == ("jaipur",)
            and wanted in JAIPUR_AMBER_NAMES and supplied in JAIPUR_AMBER_NAMES)
