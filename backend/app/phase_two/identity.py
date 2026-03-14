"""Conservative name matching, not fuzzy geographical inference."""

import re
import unicodedata


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
