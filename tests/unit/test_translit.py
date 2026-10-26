"""Latin letters for a station name in Cyrillic or Greek script (#411).

`app.network_coverage.translit` and the hub slug / short label made from it.
Invented names only (ZZ-style words written in each script), never a real
station: each case pins one rule of the table.
"""

from __future__ import annotations

import unicodedata

import pytest

from app.network_coverage import hub_derive, translit


@pytest.mark.parametrize(
    ("name", "country", "latin"),
    [
        # Bulgarian (Streamlined System): щ sht, ъ a, х h, ц ts, final -ия ia.
        ("Зищъхц", "BG", "Zishtahts"),
        ("ЗЗИЯ", "BG", "ZZIA"),
        ("Ззия Ззиява", "BG", "Zzia Zziyava"),  # -ия ia only at the end of a word
        ("Ззьо", "BG", "Zzyo"),
        # Russian (BGN/PCGN, plain letters): х kh, щ shch, ы y, ъ ь dropped, ё e.
        ("Ззхщы", "RU", "Zzkhshchy"),
        ("Ззъзьё", "RU", "Zzze"),
        ("Ззэюя", "RU", "Zzeyuya"),
        # Ukrainian (national 2010): г h, ґ g, и y, initial ye yi y yu ya,
        # elsewhere ie i i iu ia, зг zgh, the apostrophe dropped.
        ("Зугиґі", "UA", "Zuhygi"),
        ("Єзз Їзз Йзз Юзз Яззя", "UA", "Yezz Yizz Yzz Yuzz Yazzia"),
        ("Ззєїй", "UA", "Zzieii"),
        ("Зз'язг", "UA", "Zziazgh"),
        ("Зз’яз", "UA", "Zziaz"),
        # Serbian (Serbian Latin alphabet), also Montenegro and Bosnia.
        ("Ђзжћчџш", "RS", "Đzžćčdžš"),
        ("Љзњзј", "ME", "Ljznjzj"),
        ("Цзх", "BA", "Czh"),
        # Macedonian (official 2008): ѓ gj, ќ kj, ѕ dz, џ dzh, ц c.
        ("Ѓзќѕџц", "MK", "Gjzkjdzdzhc"),
        # Any other country: the Russian table, the other letters by default.
        ("Ззхій", None, "Zzkhiy"),
        ("Ззх", "ZZ", "Zzkh"),
        ("Ђѓќѕ", None, "Djgjkjdz"),
        # Greek (ELOT 743): η ι υ i i y, θ th, χ ch, ψ ps, ου ou.
        ("ΖΘΗΖΑ", None, "ZTHIZA"),
        ("Ζζχψου", None, "Zzchpsou"),
        # αυ ευ ηυ: v before a voiced sound, f before a voiceless one or at the end.
        ("Ζαυζ Ζευκ Ζηυ", None, "Zavz Zefk Zif"),
        # γγ ng, γκ gk, γξ nx, γχ nch, μπ mp; accents off; a diaeresis parts vowels.
        ("Ζγγζ Ζγκζ Ζγξζ Ζγχζ Μπζ", None, "Zngz Zgkz Znxz Znchz Mpz"),
        ("Ζάζ Ζόυ Ζοϋ Ζαϋζ", None, "Zaz Zou Zoy Zayz"),
        ("Ζσζς", "GR", "Zszs"),
    ],
)
def test_the_tables(name: str, country: str | None, latin: str) -> None:
    assert translit.to_latin(name, country) == latin


@pytest.mark.parametrize(
    "name",
    ["ZZ Zzville", "Zzé-Zzà (Zz)", "Zzł Zzß", "", "99 Zz", "L'Zzville", "Zz’zz"],
)
def test_a_latin_name_is_kept_as_it_is(name: str) -> None:
    assert translit.to_latin(name, "BG") == name
    assert translit.is_latin(name)


def test_only_the_cyrillic_and_greek_words_of_a_mixed_name_change() -> None:
    assert translit.to_latin("ZZ Ззово 99 - Ζζ", "BG") == "ZZ Zzovo 99 - Zz"
    assert not translit.is_latin("ZZ Ззово")
    assert not translit.is_latin("Ζζ")


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Zzł Zzß ẞ", "Zzl Zzss SS"),
        ("Ææ Œœ Øø", "AEae OEoe Oo"),
        ("Đđ Ðð Þþ", "Djdj Dd THth"),
        ("İı Ħħ Ŧŧ Ŋŋ", "Ii Hh Tt NGng"),
        ("Zzé Zzå", "Zze Zza"),
        ("ﬁ ²", "fi 2"),  # compatibility forms
    ],
)
def test_the_slug_folds_every_latin_letter_to_ascii(text: str, folded: str) -> None:
    assert translit.fold_ascii(text) == folded


@pytest.mark.parametrize(
    ("name", "country", "slug", "short"),
    [
        ("ЗЗИЯ", "BG", "zzia", "ZZIA"),
        ("ЗЗЩИЯ ЗЗЦЕНТРАЛНА", "BG", "zzshtia-zztsentralna", "ZZSHTIA ZZTS"),
        ("Ζζάζα Ζζ", None, "zzaza-zz", "Zzaza Zz"),
        ("Зз'язг", "UA", "zziazgh", "Zziazgh"),
        ("Ђзжћ Зз", "RS", "djzzc-zz", "Đzžć Zz"),
        ("Zzł Zzø", None, "zzl-zzo", "Zzł Zzø"),
    ],
)
def test_the_slug_and_the_short_label_are_made_from_the_latin_letters(
    name: str, country: str | None, slug: str, short: str
) -> None:
    assert hub_derive.slugify(name, country) == slug
    assert hub_derive.latin_short(name, country) == short
    assert len(hub_derive.latin_short(name, country)) <= 12


def test_a_name_with_no_letter_to_transliterate_gives_no_slug() -> None:
    assert hub_derive.slugify("〇〇", "ZZ") == ""


def test_derive_transliterates_by_the_country_it_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_derive, "country_from_coords", lambda _lat, _lon: "BG")

    out = hub_derive.derive("Ззхия", 45.0, 6.0)

    assert (out["name"], out["slug"], out["short"], out["country"]) == (
        "Ззхия",
        "zzhia",
        "Zzhia",
        "BG",
    )


# ─────────────────────────── review round 1 ───────────────────────────


@pytest.mark.parametrize(
    ("name", "country", "slug", "short"),
    [
        ("Йошкар-Ола", "RU", "yoshkar-ola", "Yoshkar-Ola"),
        ("Ѓевгелија", "MK", "gjevgelija", "Gjevgelija"),
        ("Ззїй", "UA", "zzii", "Zzii"),
        ("Ζζίζζζζζζζζζ", None, "zzizzzzzzzzz", "Zzizzzzzzzzz"),
    ],
)
def test_a_name_sent_decomposed_reads_as_its_letters(
    name: str, country: str | None, slug: str, short: str
) -> None:
    decomposed = unicodedata.normalize("NFD", name)
    assert decomposed != name
    assert hub_derive.slugify(decomposed, country) == slug
    label = hub_derive.latin_short(decomposed, country)
    assert label == short
    assert not any(unicodedata.category(ch) == "Mn" for ch in label)
    assert translit.to_latin(decomposed, country) == translit.to_latin(name, country)


def test_the_letters_with_a_grave_and_a_letter_no_table_holds() -> None:
    assert translit.to_latin("Ѐзеро", "MK") == "Ezero"
    assert translit.to_latin("Ѝзз", "BG") == "Izz"
    assert hub_derive.slugify("Ѐзеро", "MK") == "ezero"
    # A letter no table holds takes its base letter's form (Chuvash `ӑ`).
    assert translit.to_latin("Ӑзз", "RU") == "Azz"
    assert not translit.has_foreign_letters("Azz")


def test_a_letter_with_no_latin_form_is_kept_and_told() -> None:
    """Kazakh letters (KZ takes the Russian table): kept, never dropped
    silently; the picker says the slug lacks them."""
    latin = translit.to_latin("Қзғз Өзз", "KZ")
    assert latin == "Қzғz Өzz"
    assert translit.has_foreign_letters(latin)
    assert not translit.has_foreign_letters(translit.to_latin("ЗЗИЯ Ζζ", "BG"))
    assert not translit.has_foreign_letters("Zzé Zzł 99 - ()")
    assert translit.has_foreign_letters("〇〇") is False  # no letter: a placeholder


def test_a_word_mixing_scripts_or_a_dotted_capital_i() -> None:
    # `İ` lowers to two characters: no IndexError, the word stays aligned.
    assert translit.to_latin("ЗЗИЯİ", "BG") == "ZZIYAİ"
    assert translit.to_latin("Ззиİз", "RU") == "Zziİz"
    assert hub_derive.slugify("ЗЗИЯİ", "BG") == "zziyai"
    # Greek and Cyrillic in one word: each by its own rules.
    assert translit.to_latin("ΖΘБЩ", "BG") == "ZTHBSHT"
    assert translit.to_latin("Ζουщ", "RU") == "Zoushch"


def test_montenegro_takes_the_serbian_table() -> None:
    assert translit.to_latin("Ђзжћчџш", "ME") == "Đzžćčdžš"
    assert hub_derive.slugify("Ђзжћ", "ME") == "djzzc"
    # Kosovo's Cyrillic takes the default (Russian) table.
    assert translit.to_latin("Ззжћ", "XK") == "Zzzhc"


def test_a_name_of_signs_only_gives_an_empty_short_label() -> None:
    assert translit.to_latin("ЪЬ", "RU") == ""
    assert hub_derive.latin_short("ЪЬ", "RU") == ""
    assert hub_derive.slugify("ЪЬ", "RU") == ""


def test_derive_takes_the_positions_bundled_country_beyond_the_v1_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hub_derive, "country_from_coords", lambda _lat, _lon: None)
    monkeypatch.setattr(hub_derive, "country_of_position", lambda _lat, _lon: "MK")

    out = hub_derive.derive("Цзз", 45.0, 6.0)

    assert (out["slug"], out["short"], out["country"]) == ("czz", "Czz", "")
    assert hub_derive.derive("Цзз", None, None)["slug"] == "tszz"  # no position: the default
