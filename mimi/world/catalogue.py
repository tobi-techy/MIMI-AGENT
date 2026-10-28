"""Fixed reference data for the synthetic world: places, names, products.

Places are real Ado-Ekiti areas and streets; every shop, owner and brand is
invented. Brand names are made up on purpose so the demo says nothing about a
real brand. Prices are rough late-2026 Ado-Ekiti trade prices in naira,
converted to kobo by the generator.
"""

from __future__ import annotations

# (area, streets)
AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Oja Oba", ("Oja Oba Market Road", "Old Garage Street", "Okeyinmi Street")),
    ("Ajilosun", ("Ajilosun Road", "Ojo Street", "Federal Polytechnic Road")),
    ("Okesa", ("Okesa Street", "Ogbon Oja", "Odo Ijigbo Road")),
    ("Basiri", ("Basiri Road", "Olorunsogo Street", "Ebenezer Avenue")),
    ("Adebayo", ("Adebayo Road", "Iworoko Road", "Ekiti State University Road")),
    ("Fajuyi", ("Fajuyi Park Road", "Bank Road", "Secretariat Road")),
    ("Oke-Ila", ("Oke-Ila Street", "Ijomu Street", "Moferere Street")),
    ("Irona", ("Irona Road", "Ureje Road", "Ikere Road")),
    ("Odo-Ado", ("Odo-Ado Street", "Ilokun Road", "Atikankan Street")),
    ("Ilawe Road", ("Ilawe Road", "Agric Olope Road", "Dallimore Street")),
)

YORUBA_FIRST: tuple[str, ...] = (
    "Tolu",
    "Bola",
    "Bisi",
    "Funke",
    "Kemi",
    "Tunde",
    "Segun",
    "Yemisi",
    "Ronke",
    "Bayo",
    "Lola",
    "Dayo",
    "Wale",
    "Sade",
    "Femi",
    "Toyin",
    "Kunle",
    "Bukola",
    "Gbenga",
    "Titi",
    "Sola",
    "Lanre",
    "Remi",
    "Jumoke",
    "Kehinde",
    "Taiwo",
    "Yinka",
    "Abiodun",
    "Folake",
    "Moji",
)
OTHER_FIRST: tuple[str, ...] = ("Ngozi", "Chidi", "Halima", "Musa", "Ifeoma", "Emeka")
SURNAMES: tuple[str, ...] = (
    "Adeyemi",
    "Ojo",
    "Fashola",
    "Bamidele",
    "Akinola",
    "Olatunji",
    "Ogunleye",
    "Ayodele",
    "Ibitoye",
    "Fagbemi",
    "Aluko",
    "Afolabi",
    "Oyebanji",
    "Adesina",
    "Falodun",
    "Ajayi",
    "Owolabi",
    "Ogundipe",
    "Okafor",
    "Bello",
)
# Names in the local style, by shop type.
SHOP_STYLES: dict[str, tuple[str, ...]] = {
    "provision store": (
        "Mama {first} Provisions",
        "God's Grace Stores",
        "{first} Provision Store",
        "Divine Favour Provisions",
        "Iya {first} Provisions",
    ),
    "kiosk": ("{first} Kiosk", "Mama {first} Kiosk", "Olorun Ni Oba Kiosk"),
    "mini-mart": (
        "{first} & Sons Mini Mart",
        "Blessed {first} Mini Mart",
        "His Mercy Mini Mart",
        "{last} Supermarket",
    ),
    "mama-put supplier": ("Iya {first} Foodstuff", "{first} Foodstuff & Provisions"),
    "POS-plus-provisions": ("{first} POS & Provisions", "Grace Point POS & Stores"),
}
SHOP_TYPES_BY_SIZE: dict[str, tuple[str, ...]] = {
    "big": ("mini-mart", "provision store", "mama-put supplier"),
    "medium": (
        "provision store",
        "mini-mart",
        "POS-plus-provisions",
        "mama-put supplier",
    ),
    "small": ("kiosk", "provision store", "POS-plus-provisions", "kiosk"),
}

# The bank or app a transfer shop pays from. Every name is invented for the
# demo (see ``mimi.proof.banks``): three app receipts, then three SMS alerts.
BANKS: tuple[str, ...] = (
    "Lagoonpay",
    "Harmattan MFB",
    "Kestrel Bank",
    "Ashgrove Bank",
    "Marlwood Bank",
    "Ironleaf Bank",
)

# (sku, name, brand, unit, price in naira, bulk, rises mid-period)
# bulk SKUs (50kg bags, 5L oil, cartons of tins) go to big and medium shops.
CATALOGUE: tuple[tuple[str, str, str, str, int, bool, bool], ...] = (
    (
        "NDL-70-CTN",
        "Sunrise Instant Noodles 70g (ctn of 40)",
        "Sunrise",
        "ctn",
        11_800,
        False,
        True,
    ),
    (
        "NDL-120-CTN",
        "Sunrise Big Pack Noodles 120g (ctn of 40)",
        "Sunrise",
        "ctn",
        17_500,
        False,
        True,
    ),
    (
        "MLK-SCH-BAG",
        "Okin Sachet Milk 15g (bag of 10)",
        "Okin",
        "bag",
        1_500,
        False,
        True,
    ),
    (
        "MLK-SCH-CTN",
        "Okin Sachet Milk 15g (ctn of 210)",
        "Okin",
        "ctn",
        30_000,
        True,
        True,
    ),
    (
        "MLK-TIN-CTN",
        "Okin Evaporated Milk 160g (ctn of 48)",
        "Okin",
        "ctn",
        26_000,
        True,
        False,
    ),
    ("OIL-1L", "Palmside Vegetable Oil 1L", "Palmside", "btl", 3_400, False, True),
    ("OIL-5L", "Palmside Vegetable Oil 5L", "Palmside", "keg", 15_800, True, True),
    (
        "RICE-50KG",
        "Ekiti Gold Parboiled Rice 50kg",
        "Ekiti Gold",
        "bag",
        82_000,
        True,
        True,
    ),
    (
        "RICE-5KG",
        "Ekiti Gold Parboiled Rice 5kg",
        "Ekiti Gold",
        "bag",
        8_900,
        False,
        True,
    ),
    ("SUG-1KG", "Amber Granulated Sugar 1kg", "Amber", "pk", 2_100, False, False),
    ("SUG-50KG", "Amber Granulated Sugar 50kg", "Amber", "bag", 85_000, True, False),
    (
        "CUBE-100",
        "Zeta Seasoning Cubes (pack of 100)",
        "Zeta",
        "pk",
        2_300,
        False,
        False,
    ),
    (
        "CUBE-CTN",
        "Zeta Seasoning Cubes (ctn of 24 packs)",
        "Zeta",
        "ctn",
        52_000,
        True,
        False,
    ),
    ("DET-900G", "Brightwash Detergent 900g", "Brightwash", "pk", 2_800, False, False),
    (
        "DET-SCH-12",
        "Brightwash Detergent 40g sachets (pack of 12)",
        "Brightwash",
        "pk",
        1_300,
        False,
        False,
    ),
    ("SOAP-CTN", "Lilly Bar Soap (ctn of 24)", "Lilly", "ctn", 10_500, False, False),
    ("SPAG-500", "Mamaland Spaghetti 500g", "Mamaland", "pk", 1_000, False, True),
    (
        "SPAG-CTN",
        "Mamaland Spaghetti 500g (ctn of 20)",
        "Mamaland",
        "ctn",
        19_500,
        False,
        True,
    ),
    ("SEMO-1KG", "Mamaland Semolina 1kg", "Mamaland", "pk", 1_800, False, False),
    (
        "TPASTE-CTN",
        "Redland Tomato Paste 70g (ctn of 50)",
        "Redland",
        "ctn",
        12_000,
        False,
        False,
    ),
    ("SARD-125", "Seaview Sardines 125g", "Seaview", "tin", 1_200, False, False),
    (
        "COCOA-500",
        "Choco-Royal Cocoa Drink 500g refill",
        "Choco-Royal",
        "pk",
        3_900,
        False,
        True,
    ),
    (
        "MALTED-400",
        "Vita-Malt Malted Drink 400g refill",
        "Vita-Malt",
        "pk",
        3_700,
        False,
        False,
    ),
    (
        "SODA-50CL",
        "Fizzup Cola 50cl PET (pack of 12)",
        "Fizzup",
        "pk",
        5_400,
        False,
        False,
    ),
    (
        "MALT-33CL",
        "Royal Malt 33cl can (pack of 24)",
        "Royal Malt",
        "pk",
        14_500,
        False,
        False,
    ),
    (
        "WATER-75CL",
        "Ekiti Spring Water 75cl (pack of 12)",
        "Ekiti Spring",
        "pk",
        2_900,
        False,
        False,
    ),
    (
        "PWATER-BAG",
        "Ekiti Spring Sachet Water (bag of 20)",
        "Ekiti Spring",
        "bag",
        400,
        False,
        False,
    ),
    ("TPASTE-140", "Smile Toothpaste 140g", "Smile", "pc", 1_150, False, False),
    ("BISC-CTN", "Crunchy Biscuits (ctn of 24)", "Crunchy", "ctn", 6_000, False, False),
    ("CUSTARD-2KG", "Mamaland Custard 2kg", "Mamaland", "tub", 7_200, False, False),
)

REP_NAMES: tuple[str, ...] = ("Kayode Ajayi", "Amaka Nwosu")


# The account Coral Lane Trading's shops pay into (fictional bank, fictional
# number). The store seeds it, the Collector gives it to shops and the proof
# checks compare every screenshot's recipient against it.
COLLECTION_ACCOUNT: tuple[tuple[str, str], ...] = (
    ("account_name", "CORAL LANE TRADING"),
    ("bank", "Kestrel Bank"),
    ("account_number", "9990004471"),
)
