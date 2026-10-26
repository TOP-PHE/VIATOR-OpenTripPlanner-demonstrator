"""Latin letters for a station name in Cyrillic or Greek script (#411).

The station module gives a station's name in its own script (a Bulgarian
station in Cyrillic, a Greek one in Greek). The hub picker proposes a slug
and a short label from that name, and both must be readable in Latin
letters: before #411 a Cyrillic or Greek name gave no letter at all and the
proposed slug was the bare `hub`.

A small explicit table, no dependency; each rule follows a published
romanisation, simplified to plain letters where it has a plain form. The
text is first made NFC, so a name sent decomposed reads as its letters.

- Bulgarian (BG): the official Streamlined System (law of 2009): `щ` sht,
  `ъ` a, `х` h, `ц` ts, and a word ending in `ия` ends in `ia`
  (a name `СОФИЯ` gives `SOFIA`, slug `sofia`).
- Russian (RU, BY, KZ, KG) and the Cyrillic of every country not named
  here (Kosovo included): a simplified BGN/PCGN, without its apostrophes,
  its diaeresis or its `ye` for an initial `е`: `х` kh, `щ` shch, `ы` y,
  `й` y, `ъ` and `ь` dropped, `ё` e. The letters Russian lacks take the
  forms below (`і` i, `ї` yi, `є` ye, `ў` u, `ѐ` e, `ѝ` i, the Serbian
  and Macedonian ones).
- Ukrainian (UA): the official national system (Cabinet resolution 55 of
  2010): `г` h, `ґ` g, `и` y, `і` i, `х` kh, `щ` shch; `є ї й ю я` are
  `ye yi y yu ya` at the start of a word and `ie i i iu ia` elsewhere;
  `зг` is `zgh`; the apostrophe is dropped.
- Serbian (RS, ME, BA): the Serbian Latin alphabet (`ж` ž, `ћ` ć, `ђ` đ,
  `џ` dž, `љ` lj, `њ` nj), whose diacritics the slug folds (`đ` dj).
- Macedonian (MK): the official romanisation (2008): `ѓ` gj, `ќ` kj,
  `ѕ` dz, `џ` dzh, `ж` zh, `ц` c, `ч` ch, `ш` sh.
- Greek: ELOT 743 (ISO 843 transcription), simplified: `η ι υ` i i y,
  `θ` th, `χ` ch, `ψ` ps, `ου` ou, `αυ ευ ηυ` av ev iv (af ef if before a
  voiceless consonant or at the end of a word), `γγ` ng, `γξ` nx, `γχ`
  nch, and `γκ` gk, `μπ` mp wherever they stand (as on passports, not the
  forms by position); accents dropped, a diaeresis parting two vowels
  (`ΑΘΗΝΑ` gives `ATHINA`).

A Cyrillic letter no table holds takes its base letter's form (`ӑ` as `а`),
else is kept as it is: `has_foreign_letters` then tells the picker, which
says so rather than dropping the letter silently.

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
    "ѓ": "gj", "ќ": "kj", "ѕ": "dz", "ѐ": "e", "ѝ": "i",
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
    return "\u0400" <= ch <= "\u04ff"


def _is_greek(ch: str) -> bool:
    return "\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff"


def _is_latin_letter(ch: str) -> bool:
    return ch.isascii() or unicodedata.name(ch, "").startswith("LATIN ")


def _greek_base(ch: str) -> tuple[str, bool]:
    """The letter without its accents (lowercase), and whether it has a diaeresis."""
    decomposed = unicodedata.normalize("NFD", ch)
    return decomposed[0].lower(), _DIAERESIS in decomposed


def _cyrillic_piece(lows: list[str], i: int, table: dict[str, str], country: str | None) -> str:
    """The Latin letters of the Cyrillic letter at `i` of a word (lowercase)."""
    ch = lows[i]
    if country == "UA":
        if i == 0 and ch in _CYRILLIC_UA_INITIAL:
            return _CYRILLIC_UA_INITIAL[ch]
        if ch == "г" and i > 0 and lows[i - 1] == "з":
            return "gh"
    if country == "BG" and ch == "я" and i == len(lows) - 1 and i > 0 and lows[i - 1] == "и":
        return "a"
    if ch in table:
        return table[ch]
    # A letter no table holds: its base letter's form, else kept as it is.
    base = unicodedata.normalize("NFD", ch)[0]
    return table.get(base, ch)


def _greek_at(bases: list[tuple[str, bool]], j: int) -> tuple[str, bool]:
    return bases[j] if j < len(bases) else ("", False)


def _greek_two(bases: list[tuple[str, bool]], i: int) -> tuple[str, str] | None:
    """The Latin letters of the two letters at `i` read as one sound, or None."""
    base = bases[i][0]
    nxt, nxt_diaeresis = _greek_at(bases, i + 1)
    if nxt_diaeresis:
        return None
    pair = _GREEK_PAIRS.get(base + nxt)
    if pair is not None:
        return pair
    if base in _GREEK_UPSILON_VOWELS and nxt == "υ":
        following = _greek_at(bases, i + 2)[0]
        voiceless = following == "" or following in _GREEK_VOICELESS
        return _GREEK[base], "f" if voiceless else "v"
    return None


def _greek_pieces(word: str) -> list[tuple[int, str]]:
    """(index of the source letter, its Latin letters) for a run of Greek letters."""
    bases = [_greek_base(ch) for ch in word]
    pieces: list[tuple[int, str]] = []
    i = 0
    while i < len(word):
        two = _greek_two(bases, i)
        if two is None:
            pieces.append((i, _GREEK.get(bases[i][0], word[i])))
            i += 1
        else:
            pieces += [(i, two[0]), (i + 1, two[1])]
            i += 2
    return pieces


def _word_pieces(word: str, country: str | None) -> list[tuple[int, str]]:
    """(index of the source letter, its Latin letters, lowercase) for a word
    holding Cyrillic or Greek letters: each run of Greek letters by the Greek
    rules, each Cyrillic letter by the table of `country`, a Latin letter as
    it is; the apostrophes of a word with Cyrillic letters are dropped."""
    lows = [ch.lower()[:1] for ch in word]  # one character each (`İ` lowers to two)
    table = _CYRILLIC_BY_COUNTRY.get(country or "", _CYRILLIC_DEFAULT)
    cyrillic = any(_is_cyrillic(ch) for ch in word)
    pieces: list[tuple[int, str]] = []
    i = 0
    while i < len(word):
        ch = word[i]
        if _is_greek(ch):
            end = i
            while end < len(word) and _is_greek(word[end]):
                end += 1
            pieces += [(i + k, piece) for k, piece in _greek_pieces(word[i:end])]
            i = end
            continue
        if _is_cyrillic(ch):
            pieces.append((i, _cyrillic_piece(lows, i, table, country)))
        elif not (cyrillic and ch in _APOSTROPHES):
            pieces.append((i, ch))
        i += 1
    return pieces


def _word_to_latin(word: str, country: str | None) -> str:
    """One word in Latin letters, keeping its case (all capitals stay capitals)."""
    if is_latin(word):
        return word
    cased = [ch for ch in word if ch.lower() != ch.upper()]
    all_capitals = len(cased) >= 2 and all(ch.isupper() for ch in cased)
    out: list[str] = []
    for index, piece in _word_pieces(word, country):
        source = word[index]
        if source == piece:  # kept as it is, with its own case
            out.append(piece)
        elif all_capitals:
            out.append(piece.upper())
        elif source.isupper():
            out.append(piece[:1].upper() + piece[1:])
        else:
            out.append(piece)
    return "".join(out)


def to_latin(text: str, country: str | None = None) -> str:
    """`text` (made NFC first) with its Cyrillic and Greek words in Latin
    letters (the rules above; the Cyrillic table chosen by `country`, an ISO
    alpha-2 code, or the default). Everything else (Latin letters with their
    accents, digits, spaces, punctuation) is kept as it is."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    return _WORD.sub(lambda m: _word_to_latin(m.group(0), country), text)


def is_latin(text: str) -> bool:
    """Whether `text` holds no Cyrillic or Greek letter."""
    return not any(_is_cyrillic(ch) or _is_greek(ch) for ch in text)


def has_foreign_letters(text: str) -> bool:
    """Whether `text` holds a letter that is not a Latin one (what `to_latin`
    could not write in Latin letters: the slug would lose it)."""
    return any(ch.isalpha() and not _is_latin_letter(ch) for ch in text)


def fold_ascii(text: str) -> str:
    """Latin letters to plain a-z letters: the letters with no accent to strip
    by their usual ASCII forms, then compatibility forms and accents off
    (`Łódź` Lodz, `Straße` Strasse, `Ærø` AEro). Other characters are kept."""
    special = unicodedata.normalize("NFC", text).translate(_LATIN_SPECIAL_TABLE)
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", special) if unicodedata.category(ch) != "Mn"
    )
