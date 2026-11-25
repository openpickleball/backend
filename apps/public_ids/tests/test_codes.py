"""Public codes: alphabet, check character, generation, and normalization."""

import random

import pytest

from apps.public_ids import codes
from apps.public_ids.codes import ALPHABET, CodeType

# A fixed seed, so a failure is reproducible. These are made-up codes.
_RNG = random.Random(92)


def _reference_valid(text: str) -> bool:
    """Luhn mod 32 validity, written out the long way.

    Counting from the right, starting at zero, every odd position is
    doubled. A doubled value of 32 or more has its two base-32 digits
    added. The total must be a multiple of 32.
    """
    total = 0
    for position, char in enumerate(reversed(text)):
        value = ALPHABET.index(char)
        if position % 2 == 1:
            value *= 2
            value = value // 32 + value % 32
        total += value
    return total % 32 == 0


def _reference_check(text: str) -> str:
    """The one character that makes ``text`` plus it valid, by search."""
    matches = [char for char in ALPHABET if _reference_valid(text + char)]
    assert len(matches) == 1
    return matches[0]


def _random_body(length: int) -> str:
    return "".join(_RNG.choice(ALPHABET) for _ in range(length))


def _valid_compact(code_type: CodeType) -> str:
    """A made-up valid code without hyphens: prefix, random, check."""
    text = code_type.prefix + _random_body(code_type.length - 1)
    return text + _reference_check(text)


def test_alphabet_is_crockford_base32() -> None:
    assert ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    assert len(set(ALPHABET)) == 32
    for excluded in "ILOU":
        assert excluded not in ALPHABET


@pytest.mark.parametrize(
    ("code_type", "prefix", "length"),
    [
        (CodeType.PLAYER, "P", 8),
        (CodeType.CLUB, "C", 8),
        (CodeType.EVENT, "E", 8),
        (CodeType.ORGANIZER, "G", 8),
        (CodeType.MATCH, "M", 12),
        (CodeType.RESULTS_FILE, "F", 8),
    ],
)
def test_code_type_prefix_and_length(
    code_type: CodeType, prefix: str, length: int
) -> None:
    assert code_type.prefix == prefix
    assert code_type.length == length
    assert prefix in ALPHABET


def test_prefixes_are_distinct() -> None:
    prefixes = [code_type.prefix for code_type in CodeType]

    assert len(set(prefixes)) == len(prefixes)


@pytest.mark.parametrize(
    ("text", "check"),
    [("P0000000", "A"), ("P7K3MQX9", "H"), ("M9QF6RD2KX3B", "P")],
)
def test_check_character_known_vectors(text: str, check: str) -> None:
    assert _reference_check(text) == check
    assert codes.check_character(text) == check


def test_check_character_matches_reference() -> None:
    for _ in range(500):
        text = _RNG.choice("PCEGM") + _random_body(_RNG.randint(1, 15))

        assert codes.check_character(text) == _reference_check(text)


def test_is_valid_matches_reference() -> None:
    for _ in range(500):
        text = _random_body(_RNG.randint(2, 16))

        assert codes.is_valid(text) == _reference_valid(text)


def test_is_valid_accepts_text_with_its_check_character() -> None:
    text = "C4M2Z9Q"

    assert codes.is_valid(text + codes.check_character(text))


def test_every_single_substitution_fails_the_check() -> None:
    for code_type in CodeType:
        compact = _valid_compact(code_type)
        for position, original in enumerate(compact):
            for other in ALPHABET:
                if other == original:
                    continue
                changed = compact[:position] + other + compact[position + 1 :]

                assert not codes.is_valid(changed)


def test_most_adjacent_transpositions_fail_the_check() -> None:
    caught = 0
    total = 0
    for _ in range(200):
        compact = _valid_compact(_RNG.choice(list(CodeType)))
        for position in range(len(compact) - 1):
            first, second = compact[position], compact[position + 1]
            if first == second:
                continue
            swapped = compact[:position] + second + first + compact[position + 2 :]
            total += 1
            caught += not codes.is_valid(swapped)

    assert caught / total >= 0.95


def test_check_covers_the_prefix() -> None:
    body = "7K3MQX9"
    player = "P" + body + codes.check_character("P" + body)

    assert not codes.is_valid("C" + player[1:])


@pytest.mark.parametrize("code_type", list(CodeType))
def test_generate_returns_a_valid_canonical_code(code_type: CodeType) -> None:
    code = codes.generate(code_type)

    prefix, *groups = code.split("-")
    assert prefix == code_type.prefix
    assert all(len(group) == 4 for group in groups)
    body = "".join(groups)
    assert len(body) == code_type.length
    assert all(char in ALPHABET for char in body)
    assert codes.is_valid(prefix + body)
    assert codes.normalize(code, code_type) == code


def test_generate_lengths() -> None:
    assert len(codes.generate(CodeType.PLAYER)) == 11
    assert len(codes.generate(CodeType.MATCH)) == 16


def test_generate_draws_from_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_choice(sequence: str) -> str:
        calls.append(sequence)
        return "Z"

    monkeypatch.setattr("apps.public_ids.codes.secrets.choice", fake_choice)

    code = codes.generate(CodeType.PLAYER)

    assert calls == [ALPHABET] * 7
    assert code.startswith("P-ZZZZ-ZZZ")


def test_generate_gives_distinct_codes() -> None:
    generated = {codes.generate(CodeType.PLAYER) for _ in range(1000)}

    assert len(generated) == 1000


def test_format_code_groups_the_body() -> None:
    assert codes.format_code(CodeType.PLAYER, "7k3mqx9h") == "P-7K3M-QX9H"
    assert codes.format_code(CodeType.MATCH, "9QF6RD2KX3BP") == "M-9QF6-RD2K-X3BP"


# normalize


def test_normalize_accepts_the_canonical_form() -> None:
    assert codes.normalize("P-7K3M-QX9H", CodeType.PLAYER) == "P-7K3M-QX9H"


@pytest.mark.parametrize(
    "raw",
    [
        "p-7k3m-qx9h",
        "P7K3MQX9H",
        "p7k3mqx9h",
        " P 7K3M QX9H ",
        "P--7K3M--QX9H",
        "7K3M-QX9H",
        "7k3mqx9h",
    ],
)
def test_normalize_accepts_loose_forms(raw: str) -> None:
    assert codes.normalize(raw, CodeType.PLAYER) == "P-7K3M-QX9H"


def test_normalize_maps_i_l_and_o() -> None:
    # The valid code is P-1101-0105: each 1 is written as I or L, each 0 as O.
    assert codes.normalize("PILOLoiO5", CodeType.PLAYER) == "P-1101-0105"


def test_normalize_matches_a_match_code() -> None:
    assert codes.normalize("m9qf6rd2kx3bp", CodeType.MATCH) == "M-9QF6-RD2K-X3BP"


@pytest.mark.parametrize(
    "raw",
    [
        # Wrong prefix for the endpoint.
        "C-7K3M-QX9H",
        "M-7K3M-QX9H",
        # One wrong character.
        "P-7K3M-QX9J",
        "P-8K3M-QX9H",
        # U is not in the alphabet.
        "P-7K3M-QXUH",
        # Wrong lengths.
        "P-7K3M-QX9",
        "P-7K3M-QX9H6",
        "",
        "P",
        "-",
        # A 6-character external player ID.
        "7K3MQX",
        "AB12CD",
        # A player code on the match endpoint has the wrong length.
        "P7K3MQX9H000",
        # Other characters.
        "P-7K3M_QX9H",
        "P.7K3M.QX9H",
        "P-7K3M-QX9\N{LATIN SMALL LETTER DOTLESS I}",
        "P-7K3M-QX9H\N{EM SPACE}",
        "\N{FULLWIDTH LATIN CAPITAL LETTER P}7K3MQX9H",
        # Too long, even if mostly separators.
        "P-7K3M-QX9H" + " " * 60,
    ],
)
def test_normalize_rejects_bad_player_input(raw: str) -> None:
    assert codes.normalize(raw, CodeType.PLAYER) is None


def test_normalize_rejects_dotless_i_before_case_folding() -> None:
    # Uppercasing a dotless i gives I, which would map to 1.
    text = "P7K3MQX1"
    code = text + codes.check_character(text)
    unicode_form = code.replace("1", "\N{LATIN SMALL LETTER DOTLESS I}")

    assert codes.normalize(code, CodeType.PLAYER) is not None
    assert codes.normalize(unicode_form, CodeType.PLAYER) is None


@pytest.mark.parametrize("raw", [None, 7, b"P7K3MQX9H", ["P7K3MQX9H"]])
def test_normalize_rejects_non_strings(raw: object) -> None:
    assert codes.normalize(raw, CodeType.PLAYER) is None  # type: ignore[arg-type]


def test_normalize_accepts_at_most_64_characters() -> None:
    padded = "P-7K3M-QX9H".ljust(64)

    assert codes.normalize(padded, CodeType.PLAYER) == "P-7K3M-QX9H"
    assert codes.normalize(padded + " ", CodeType.PLAYER) is None


def test_normalize_wrong_prefix_for_each_type() -> None:
    for code_type in CodeType:
        compact = _valid_compact(code_type)
        for other in CodeType:
            if other.length != code_type.length or other is code_type:
                continue

            assert codes.normalize(compact, other) is None
