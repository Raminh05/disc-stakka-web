"""What a disc holds, and - for a game - what it plays on.

The catalogue used to classify discs by physical format (CD / DVD / Blu-ray /
Data), which tells you nothing you want to know when you are looking for
something to put on. Discs are classified by content instead, and games carry a
second axis: the console the disc is for, since "Wii" is the question actually
being asked in front of a shelf of a hundred games.

Both are closed lists. The PS3's browser has no <datalist>, so free text would
be typed blind on a television from a game pad - and a filter is only worth
having if the values are consistent enough to group by. A <select> gives both,
and "Other" catches whatever the lists missed.
"""

GAMES = "Video games"

CATEGORIES = [GAMES, "Music", "Movies", "Software", "Other"]

# Disc-based systems only: a cartridge console can never turn up in a disc
# carousel. "PC" and "Mac" cover CD/DVD-ROM releases.
PLATFORMS = [
    "PC",
    "Mac",
    "PS1",
    "PS2",
    "PS3",
    "PS4",
    "PS5",
    "Xbox",
    "Xbox 360",
    "Xbox One",
    "Xbox Series",
    "Wii",
    "Wii U",
    "GameCube",
    "Dreamcast",
    "Saturn",
    "Other",
]


def _match(value, known):
    """Canonical spelling of `value`, or None when it is not in `known`.

    Case-insensitive because these arrive from query strings as much as from
    the form, and a hand-typed ?category=music should still work.
    """
    value = (value or "").strip()
    for candidate in known:
        if value.lower() == candidate.lower():
            return candidate
    return None


def category(value):
    """Canonical category, or None for anything we do not recognise."""
    return _match(value, CATEGORIES)


def platform(category_value, value):
    """Canonical console, or None.

    A console only means something for a game, so it is dropped for every other
    category. Without that, a disc reclassified from a game to a film would
    quietly stay a Wii disc and keep turning up under that console.
    """
    if category_value != GAMES:
        return None
    return _match(value, PLATFORMS)
