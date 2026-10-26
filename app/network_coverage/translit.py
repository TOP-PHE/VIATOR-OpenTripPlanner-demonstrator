"""Latin letters for a station name in Cyrillic or Greek script (#411).

The station module gives a station's name in its own script (a Bulgarian
station in Cyrillic, a Greek one in Greek). The hub picker proposes a slug
and a short label from that name, and both must be readable in Latin
letters: before #411 a Cyrillic or Greek name gave no letter at all and the
proposed slug was the bare `hub`.

A small explicit table, no dependency; every rule is a published national
romanisation, without its diacritics where it has a plain letter form:

- Bulgarian (BG): the official Streamlined System (law of 2009): `щ` sht,
  `ъ` a, `х` h, `ц` ts, and a word ending in `ия` ends in `ia`
  (a name `СОФИЯ` gives `SOFIA`, slug `sofia`).
- Russian (RU, BY, KZ, KG) and any Cyrillic of a country not named here:
  BGN/PCGN without its apostrophes and diaeresis: `х` kh, `щ` shch, `ы` y,
  `й` y, `ъ` and `ь` dropped, `ё` e. The letters Russian lacks take the
  forms below (`і` i, `ї` yi, `є` ye, `ў` u, Serbian and Macedonian ones).
- Ukrainian (UA): the official national system (Cabinet resolution 55 of
  2010): `г` h, `ґ` g, `и` y, `і` i, `х` kh, `щ` shch; `є ї й ю я` are
  `ye yi y yu ya` at the start of a word and `ie i i iu ia` elsewhere;
  `зг` is `zgh`; the apostrophe is dropped.
- Serbian (RS, ME, BA): the Serbian Latin alphabet (`ж` ž, `ћ` ć, `ђ` đ,
  `џ` dž, `љ` lj, `њ` nj), whose diacritics the slug folds (`đ` dj).
- Macedonian (MK): the official romanisation (2008): `ѓ` gj, `ќ` kj,
  `ѕ` dz, `џ` dzh, `ж` zh, `ц` c, `ч` ch, `ш` sh.
- Greek: ELOT 743 (ISO 843 transcription), the Greek state's own: `η ι υ`
  i i y, `θ` th, `χ` ch, `ψ` ps, `ου` ou, `αυ ευ ηυ` av ev iv (af ef if
  before a voiceless consonant or at the end of a word), `γγ` ng, `γκ` gk,
  `γξ` nx, `γχ` nch, `μπ` mp; accents dropped, a diaeresis parting two
  vowels (`ΑΘΗΝΑ` gives `ATHINA`).

Latin letters are never changed here (a Latin name keeps its accents for the
short label); `fold_ascii` is the slug's step that brings every Latin letter
to a-z, including the letters with no accent to strip (`ß` ss, `ł` l,
`æ` ae, `ø` o, `þ` th, `ı` i, …).

Pure functions, no I/O.
"""

from __future__ import annotations

import re
import unicodedata

# ─────────────────────────────── Cyrillic ───────────────────────────────

# Russian (BGN/PCGN, plain letters) and the default for any Cyrillic letter a
# country's own table does not hold.
_CYRILLIC_DEFAULT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Letters Russian lacks.
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
    "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz",
    "ѓ": "gj", "ќ": "kj", "ѕ": "dz",
}  # fmt: skip

_CYRILLIC_BG: dict[str, str] = {
    **_CYRILLIC_DEFAULT,
    "х": "h", "щ": "sht", "ъ": "a", "ь": "y",
}  # fmt: skip

_CYRILLIC_UA: dict[str, str] = {
    **_CYRILLIC_DEFAULT,
    "г": "h", "ґ": "g", "и": "y", "і": "i",
    "є": "ie", "ї": "i", "й": "i", "ю": "iu", "я": "ia",
}  # fmt: skip
# Ukrainian: the forms at the start of a word.
_CYRILLIC_UA_INITIAL: dict[str, str] = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}

_CYRILLIC_SR: dict[str, str] = {
    **_CYRILLIC_DEFAULT,
    "ђ": "đ", "ж": "ž", "ћ": "ć", "ц": "c", "ч": "č", "џ": "dž", "ш": "š",
    "х": "h", "ј": "j", "љ": "lj", "њ": "nj",
}  # fmt: skip

_CYRILLIC_MK: dict[str, str] = {
    **_CYRILLIC_DEFAULT,
    "ѓ": "gj", "ќ": "kj", "ѕ": "dz", "џ": "dzh", "ц": "c", "х": "h",
    "ј": "j", "љ": "lj", "њ": "nj",
}  # fmt: skip

# The table of a country (ISO 3166-1 alpha-2); any other: the default.
_CYRILLIC_BY_COUNTRY: dict[str, dict[str, str]] = {
    "BG": _CYRILLIC_BG,
    "UA": _CYRILLIC_UA,
    "RS": _CYRILLIC_SR,
    "ME": _CYRILLIC_SR,
    "BA": _CYRILLIC_SR,
    "MK": _CYRILLIC_MK,
}

# ──────────────────────────────── Greek ────────────────────────────────

_GREEK: dict[str, str] = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}  # fmt: skip
# Two letters read as one sound (the second without a diaeresis).
_GREEK_PAIRS: dict[str, tuple[str, str]] = {
    "ου": ("o", "u"),
    "γγ": ("n", "g"),
    "γκ": ("g", "k"),
    "γξ": ("n", "x"),
    "γχ": ("n", "ch"),
    "μπ": ("m", "p"),
}
# `αυ ευ ηυ`: `υ` is f before these (voiceless) or at the end of a word, else v.
_GREEK_VOICELESS = frozenset("θκξπστφχψς")
_GREEK_UPSILON_VOWELS = frozenset("αεη")
_DIAERESIS = "̈"

# ───────────────────────────── special Latin ─────────────────────────────

# The Latin letters that have no accent to strip (NFKD keeps them whole).
_LATIN_SPECIAL: dict[str, str] = {
    "ß": "ss", "ẞ": "SS", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "Ø": "O", "ø": "o", "Ł": "L", "ł": "l", "Đ": "Dj", "đ": "dj",
    "Ð": "D", "ð": "d", "Þ": "TH", "þ": "th", "ı": "i", "Ħ": "H", "ħ": "h",
    "Ŧ": "T", "ŧ": "t", "Ŋ": "NG", "ŋ": "ng",
}  # fmt: skip
_LATIN_SPECIAL_TABLE = str.maketrans(_LATIN_SPECIAL)

# A word: letters, with the apostrophes inside it (Ukrainian `м'я`).
_WORD = re.compile(r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*")
_APOSTROPHES = frozenset("'’ʼ")


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _is_greek(ch: str) -> bool:
    return "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿"


def _greek_base(ch: str) -> tuple[str, bool]:
    """The letter without its accents (lowercase), and whether it has a diaeresis."""
    decomposed = unicodedata.normalize("NFD", ch)
    return decomposed[0].lower(), _DIAERESIS in decomposed


def _cyrillic_piece(lower: str, i: int, table: dict[str, str], country: str | None) -> str:
    """The Latin letters of the Cyrillic letter at `i` of a word (lowercase)."""
    ch = lower[i]
    if country == "UA":
        if i == 0 and ch in _CYRILLIC_UA_INITIAL:
            return _CYRILLIC_UA_INITIAL[ch]
        if ch == "г" and i > 0 and lower[i - 1] == "з":
            return "gh"
    if country == "BG" and ch == "я" and i == len(lower) - 1 and i > 0 and lower[i - 1] == "и":
        return "a"
    return table.get(ch, ch)


def _greek_pieces(word: str) -> list[tuple[int, str]]:
    """(index of the first source letter, its Latin letters) for a Greek word."""
    bases = [_greek_base(ch) for ch in word]
    pieces: list[tuple[int, str]] = []
    i = 0
    while i < len(word):
        base, _ = bases[i]
        nxt, nxt_diaeresis = bases[i + 1] if i + 1 < len(word) else ("", False)
        pair = _GREEK_PAIRS.get(base + nxt) if not nxt_diaeresis else None
        if pair is not None:
            pieces.append((i, pair[0]))
            pieces.append((i + 1, pair[1]))
            i += 2
            continue
        if base in _GREEK_UPSILON_VOWELS and nxt == "υ" and not nxt_diaeresis:
            following = bases[i + 2][0] if i + 2 < len(word) else ""
            voiceless = following == "" or following in _GREEK_VOICELESS
            pieces.append((i, _GREEK[base]))
            pieces.append((i + 1, "f" if voiceless else "v"))
            i += 2
            continue
        pieces.append((i, _GREEK.get(base, word[i])))
        i += 1
    return pieces


def _word_to_latin(word: str, country: str | None) -> str:
    """One word in Latin letters, keeping its case (all capitals stay capitals)."""
    if not any(_is_cyrillic(ch) or _is_greek(ch) for ch in word):
        return word
    lower = word.lower()
    if any(_is_greek(ch) for ch in word):
        pieces = _greek_pieces(word)
    else:
        table = _CYRILLIC_BY_COUNTRY.get(country or "", _CYRILLIC_DEFAULT)
        # The apostrophe of a Cyrillic word is dropped; a Latin letter in it is kept.
        pieces = [
            (i, _cyrillic_piece(lower, i, table, country) if _is_cyrillic(ch) else word[i])
            for i, ch in enumerate(lower)
            if ch not in _APOSTROPHES
        ]
    cased = [ch for ch in word if ch.lower() != ch.upper()]
    all_capitals = len(cased) >= 2 and all(ch.isupper() for ch in cased)
    out: list[str] = []
    for index, piece in pieces:
        if all_capitals:
            out.append(piece.upper())
        elif word[index].isupper():
            out.append(piece[:1].upper() + piece[1:])
        else:
            out.append(piece)
    return "".join(out)


def to_latin(text: str, country: str | None = None) -> str:
    """`text` with its Cyrillic and Greek words in Latin letters (the rules
    above; the Cyrillic table chosen by `country`, an ISO alpha-2 code, or
    the default). Everything else (Latin letters with their accents, digits,
    spaces, punctuation) is kept as it is."""
    if not text:
        return ""
    return _WORD.sub(lambda m: _word_to_latin(m.group(0), country), text)


def is_latin(text: str) -> bool:
    """Whether `text` holds no Cyrillic or Greek letter."""
    return not any(_is_cyrillic(ch) or _is_greek(ch) for ch in text)


def fold_ascii(text: str) -> str:
    """Latin letters to plain a-z letters: the letters with no accent to strip
    by their usual ASCII forms, then compatibility forms and accents off
    (`Łódź` Lodz, `Straße` Strasse, `Ærø` AEro). Other characters are kept."""
    special = text.translate(_LATIN_SPECIAL_TABLE)
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", special) if unicodedata.category(ch) != "Mn"
    )
