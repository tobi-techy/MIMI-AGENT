"""What shops call each product, and how a line of words finds its sku (RAI-302).

Code, not the model, decides which catalogue item a line is. Shops name
goods by the word everyone uses ("Indomie" for any noodles, "Omo" for
detergent, "Maggi" for cubes, "Peak" for milk, "pure water" for sachet
water), so the distributor keeps a nickname list per product family. The
synthetic Coral Lane catalogue uses invented brands; the nicknames are the
words shops say, and every confirmation names the catalogue item, so a shop
and the owner always see what was matched.

``match`` takes the line's words (the item, the quote, the unit the shop
said) and the skus the shop has bought before:

1. Family: the longest nickname phrase found in the words. None found: not
   carried. Two families tied: unclear.
2. Variant: size and pack words inside the family ("big", "120g", "sachet",
   "5kg", "keg") keep the variants they name.
3. Unit: the unit the shop said ("carton", "bag", "pack", "keg") keeps the
   variants sold in that unit. A unit no variant is sold in is unclear (one
   question), never a guess.
4. History: when two or more variants are left, the shop's usual one (the
   only one of them it has bought) settles it; otherwise one question.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from mimi.orders.words import fold

# Unit words as shops write and say them -> the catalogue unit.
UNIT_SYNONYMS: dict[str, str] = {
    **{w: "ctn" for w in ("carton", "cartons", "ctn", "ctns", "katon", "katons", "paali",
                          "pali", "kato", "box", "boxes", "cartoon", "cartoons")},
    **{w: "bag" for w in ("bag", "bags", "baagi", "sack", "sacks")},
    **{w: "pk" for w in ("pack", "packs", "pk", "pks", "pkt", "pkts", "packet", "packets",
                         "pakiti", "paketi", "refill", "refills")},
    **{w: "btl" for w in ("bottle", "bottles", "btl", "btls", "igo")},
    **{w: "keg" for w in ("keg", "kegs", "gallon", "gallons", "galon", "jerrycan",
                          "jerrycans", "jerican", "jericans", "jarikan")},
    **{w: "tin" for w in ("tin", "tins")},
    **{w: "pc" for w in ("piece", "pieces", "pc", "pcs")},
    **{w: "tub" for w in ("tub", "tubs", "rubber", "rubbers", "bucket", "buckets")},
}


@dataclass(frozen=True)
class Family:
    key: str
    label: str  # how a question names it ("noodles")
    words: tuple[str, ...]  # nickname phrases, folded
    # sku -> variant words, folded. A family of one sku has none.
    variants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


FAMILIES: tuple[Family, ...] = (
    Family("noodles", "noodles", ("indomie", "indomi", "noodle", "noodles", "sunrise",
                                  "instant noodles", "mimee", "minimie"),
           {"NDL-70-CTN": ("70g", "small", "regular", "normal", "ordinary", "small one",
                           "small size", "single"),
            "NDL-120-CTN": ("120g", "big", "big pack", "super", "super pack", "superpack",
                            "hungry man", "large", "big one", "belle full")}),
    Family("milk", "milk", ("milk", "peak", "peak milk", "okin", "evaporated milk",
                            "milk tin", "tin milk", "sachet milk"),
           {"MLK-SCH-BAG": ("sachet", "sachets", "satchet", "15g", "powder", "small",
                            "sachet milk"),
            "MLK-SCH-CTN": ("sachet", "sachets", "satchet", "15g", "powder", "sachet milk"),
            "MLK-TIN-CTN": ("tin", "tins", "tinned", "evaporated", "160g", "liquid",
                            "can", "tin milk", "milk tin")}),
    Family("oil", "vegetable oil", ("oil", "vegetable oil", "veg oil", "cooking oil",
                                    "palmside", "groundnut oil", "turkey oil"),
           {"OIL-1L": ("1l", "bottle", "bottles", "igo", "small"),
            "OIL-5L": ("5l", "keg", "kegs", "gallon", "jerrycan", "big")}),
    Family("rice", "rice", ("rice", "ekiti gold", "parboiled rice", "foreign rice",
                            "local rice"),
           {"RICE-50KG": ("50kg", "big bag", "full bag", "big"),
            "RICE-5KG": ("5kg", "small bag", "small")}),
    Family("sugar", "sugar", ("sugar", "amber", "granulated sugar"),
           {"SUG-1KG": ("1kg", "small"), "SUG-50KG": ("50kg", "big")}),
    Family("cubes", "seasoning cubes", ("maggi", "knorr", "cube", "cubes", "seasoning",
                                        "seasoning cubes", "zeta", "royco"),
           {"CUBE-100": ("100",), "CUBE-CTN": ("24 packs",)}),
    Family("detergent", "detergent", ("omo", "detergent", "washing powder", "soap powder",
                                      "brightwash", "ariel", "sunlight powder"),
           {"DET-900G": ("900g", "big", "large", "big one"),
            "DET-SCH-12": ("sachet", "sachets", "satchet", "40g", "small", "small one")}),
    Family("soap", "bar soap", ("bar soap", "soap", "lilly", "key soap", "laundry soap",
                                "soap bar"), {}),
    Family("spaghetti", "spaghetti", ("spaghetti", "spag", "spagetti", "spaghetti 500g",
                                      "pasta", "mamaland spaghetti"),
           {"SPAG-500": (), "SPAG-CTN": ()}),
    Family("semolina", "semolina", ("semo", "semolina", "semovita"), {}),
    Family("tomato", "tomato paste", ("tomato paste", "tomatoes paste", "tomato", "tomatoes",
                                      "tin tomato", "tomato puree", "redland"), {}),
    Family("toothpaste", "toothpaste", ("toothpaste", "tooth paste", "smile toothpaste"), {}),
    Family("sardines", "sardines", ("sardine", "sardines", "titus", "seaview"), {}),
    Family("cocoa", "cocoa drink", ("cocoa", "choco", "choco royal", "cocoa drink",
                                    "chocolate drink"), {}),
    Family("malted", "malted drink", ("vita malt", "vitamalt", "malted drink", "malted",
                                      "malted powder"), {}),
    Family("malt", "malt drink", ("malt", "royal malt", "malta", "malt drink", "can malt",
                                  "malt can"), {}),
    Family("cola", "cola", ("cola", "coke", "fizzup", "soda", "coca cola", "pepsi",
                            "minerals", "soft drink"), {}),
    Family("water", "water", ("water", "pure water", "sachet water", "water sachet",
                              "bottle water", "water bottle", "bottled water", "table water",
                              "eva water", "spring water", "purewater"),
           {"WATER-75CL": ("bottle", "bottles", "bottled", "75cl", "table water", "eva",
                           "water bottle", "bottle water"),
            "PWATER-BAG": ("pure", "sachet", "pure water", "sachet water", "purewater")}),
    Family("biscuits", "biscuits", ("biscuit", "biscuits", "crunchy", "crunchy biscuits"), {}),
    Family("custard", "custard", ("custard", "mamaland custard"), {}),
)

# Which family each catalogue sku belongs to (skus missing here are only
# matched by their own name words).
SKU_FAMILY: dict[str, str] = {
    "NDL-70-CTN": "noodles", "NDL-120-CTN": "noodles", "MLK-SCH-BAG": "milk",
    "MLK-SCH-CTN": "milk", "MLK-TIN-CTN": "milk", "OIL-1L": "oil", "OIL-5L": "oil",
    "RICE-50KG": "rice", "RICE-5KG": "rice", "SUG-1KG": "sugar", "SUG-50KG": "sugar",
    "CUBE-100": "cubes", "CUBE-CTN": "cubes", "DET-900G": "detergent",
    "DET-SCH-12": "detergent", "SOAP-CTN": "soap", "SPAG-500": "spaghetti",
    "SPAG-CTN": "spaghetti", "SEMO-1KG": "semolina", "TPASTE-CTN": "tomato",
    "SARD-125": "sardines", "COCOA-500": "cocoa", "MALTED-400": "malted",
    "SODA-50CL": "cola", "MALT-33CL": "malt", "WATER-75CL": "water", "PWATER-BAG": "water",
    "TPASTE-140": "toothpaste", "BISC-CTN": "biscuits", "CUSTARD-2KG": "custard",
}
BY_KEY: dict[str, Family] = {f.key: f for f in FAMILIES}

MatchKind = Literal["one", "several", "none", "unit"]


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    skus: tuple[str, ...] = ()
    family: str = ""
    via: str = ""  # words | history | only

    @property
    def sku(self) -> str:
        return self.skus[0] if self.kind == "one" else ""


def _has(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def unit_of(words: str) -> str:
    """The catalogue unit a unit word names ("cartons" -> "ctn"), or ""."""
    for w in fold(words).split():
        if w in UNIT_SYNONYMS:
            return UNIT_SYNONYMS[w]
    return ""


def family_of(text: str) -> tuple[list[str], int]:
    """(families whose longest nickname is found, that length in words)."""
    t = fold(text)
    best, keys = 0, []
    for f in FAMILIES:
        n = max((len(p.split()) for p in f.words if _has(t, p)), default=0)
        if n == 0:
            continue
        if n > best:
            best, keys = n, [f.key]
        elif n == best:
            keys.append(f.key)
    return keys, best


_SIZE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:g|kg|l|cl|ml)\b")
_PACK_WORDS = ("sachet", "sachets", "tin", "can", "refill", "pet", "bottle", "big pack")


@dataclass(frozen=True)
class BookNames:
    """The distributor's own product names, read from its book (RAI-314).

    The nickname families above are the words every shop uses; this is the
    second layer: each distributor's brands, sizes and pack words, built from
    the skus in its book, never typed in by hand. It is only used when the
    families find nothing ("Smile", "Mamaland 2kg", "Ekiti Spring 75cl"), so
    a line the families already read is read the same way.
    """

    brands: Mapping[str, tuple[str, ...]]  # folded brand phrase -> skus
    sizes: Mapping[str, tuple[str, ...]]  # sku -> size tokens ("2kg", "75cl")
    packs: Mapping[str, tuple[str, ...]]  # sku -> pack words in its name

    @classmethod
    def from_skus(cls, skus: Iterable[Any]) -> BookNames:
        brands: dict[str, list[str]] = {}
        sizes: dict[str, tuple[str, ...]] = {}
        packs: dict[str, tuple[str, ...]] = {}
        for s in skus:
            b = fold(str(s.brand).replace("-", " "))
            if b:
                brands.setdefault(b, []).append(s.sku)
            name = fold(str(s.name).replace("-", " "))
            sizes[s.sku] = tuple(sorted({t.replace(" ", "") for t in _SIZE.findall(name)}))
            packs[s.sku] = tuple(w for w in _PACK_WORDS if _has(name, w))
        return cls({k: tuple(v) for k, v in sorted(brands.items())}, sizes, packs)

    def match(self, text: str, unit_word: str, units: Mapping[str, str]) -> Match | None:
        """The skus a line names by brand, or None when it names no brand."""
        t = fold(text.replace("-", " "))
        found = [b for b in self.brands if _has(t, b)]
        if not found:
            return None
        cands = sorted({s for b in found for s in self.brands[b] if s in units})
        if not cands:
            return None
        squash = t.replace(" ", "")
        sized = [s for s in cands if any(z in squash for z in self.sizes.get(s, ()))]
        cands = sized or cands
        packed = [s for s in cands if any(_has(t, w) for w in self.packs.get(s, ()))]
        cands = packed or cands
        unit = UNIT_SYNONYMS.get(fold(unit_word), "") or unit_of(unit_word)
        if unit:
            in_unit = [s for s in cands if units[s] == unit]
            if not in_unit:
                return Match("unit", tuple(cands), SKU_FAMILY.get(cands[0], ""), "book")
            cands = in_unit
        if len(cands) == 1:
            return Match("one", (cands[0],), SKU_FAMILY.get(cands[0], ""), "book")
        return Match("several", tuple(cands), "+".join(sorted({SKU_FAMILY.get(s, s)
                                                               for s in cands})), "book")


def match(item: str, unit_word: str, units: Mapping[str, str],
          bought: Iterable[str] = (), book: BookNames | None = None) -> Match:
    """Which sku a line names. ``units`` is the catalogue's sku -> unit.

    ``book`` (the distributor's own names) is read only when the nickname
    families find nothing."""
    m = _match(item, unit_word, units, bought)
    if m.kind == "none" and book is not None:
        return book.match(item, unit_word, units) or m
    return m


def _match(item: str, unit_word: str, units: Mapping[str, str],
           bought: Iterable[str] = ()) -> Match:
    text = fold(item)
    keys, _ = family_of(text)
    if not keys:
        return Match("none")
    if len(keys) > 1:
        skus = tuple(s for s, f in SKU_FAMILY.items() if f in keys and s in units)
        return Match("several", skus, "+".join(keys), "families")
    fam = BY_KEY[keys[0]]
    cands = [s for s, f in SKU_FAMILY.items() if f == fam.key and s in units]
    if not cands:
        return Match("none", family=fam.key)
    hits = {s: sum(1 for w in fam.variants.get(s, ()) if _has(text, w)) for s in cands}
    top = max(hits.values(), default=0)
    variant_words: set[str] = set()
    if top > 0:
        cands = [s for s in cands if hits[s] == top]
        for s in cands:
            for w in fam.variants.get(s, ()):
                if _has(text, w):
                    variant_words |= set(w.split())
    unit = UNIT_SYNONYMS.get(fold(unit_word), "") or unit_of(unit_word)
    if unit:
        sold = [s for s in cands if units[s] == unit]
        if sold:
            cands = sold
        elif not any(w for w in fold(unit_word).split() if w in variant_words):
            # Said in a unit none of these is sold in ("3 carton of malt" when
            # malt comes in packs): ask, never guess.
            return Match("unit", tuple(cands), fam.key, "unit")
    if len(cands) == 1:
        return Match("one", (cands[0],), fam.key, "words" if top or unit else "only")
    usual = [s for s in cands if s in set(bought)]
    if len(usual) == 1:
        return Match("one", (usual[0],), fam.key, "history")
    return Match("several", tuple(cands), fam.key, "ask")
