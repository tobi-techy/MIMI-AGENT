"""The shop-to-Zoho customer matcher, on its own (no store, no transport).

The agreement screen and an owner-approved payment both resolve a shop the
same way, so the rules are pinned here: ``zc-`` first, then one exact
(case and space folded) name, and never a name two customers share.
"""

from __future__ import annotations

from mimi.api.zoho_match import ZohoCustomer, match_shops, norm_name


def test_norm_name_folds_case_and_spacing_only() -> None:
    assert norm_name("  Mama   Sola\tProvisions \n") == "mama sola provisions"
    assert norm_name("Funke & Sons Mini Mart") == "funke & sons mini mart"
    assert norm_name(None) == ""  # type: ignore[arg-type]


def test_the_fake_id_convention_wins_and_still_sums() -> None:
    """The ``zc-`` path is unchanged: same id wins over a name, and two
    contacts carrying one shop's id still add up."""
    shops = {"shop-001": "Mama Sola Provisions"}
    out = match_shops(shops, [
        ZohoCustomer("zc-shop-001", "Whatever", 1_000),
        ZohoCustomer("zc-shop-001", "Mama Sola Provisions", 2_000),
        ZohoCustomer("5139", "Mama Sola Provisions", 9_999),
    ])
    assert out.per_shop == {"shop-001": 3_000}
    assert out.matched_by == {"shop-001": "id"}
    assert out.by_id == {"shop-001": "zc-shop-001"}
    # The named customer no longer matches the shop, so its money keeps a row.
    assert out.zoho_only == {"zoho:Mama Sola Provisions": ("Mama Sola Provisions", 9_999)}


def test_a_name_matches_only_one_customer_and_one_shop() -> None:
    shops = {"shop-040": "Coral Lane Trading"}
    out = match_shops(shops, [ZohoCustomer("5139", " coral  lane trading ", 500)])
    assert out.per_shop == {"shop-040": 500}
    assert out.matched_by == {"shop-040": "name"}


def test_two_customers_with_one_name_match_nobody() -> None:
    shops = {"shop-040": "Coral Lane Trading"}
    out = match_shops(shops, [
        ZohoCustomer("5139", "Coral Lane Trading", 500),
        ZohoCustomer("5140", "coral lane   trading", 700),
    ])
    assert out.per_shop == {}
    assert out.ambiguous == ("shop-040",)
    assert set(out.zoho_only.values()) == {("Coral Lane Trading", 500),
                                           ("coral lane   trading", 700)}


def test_two_shops_with_one_name_match_nobody() -> None:
    """The mirror case: one Zoho customer cannot stand for two Mimi shops."""
    shops = {"shop-001": "Mama Sola Provisions", "shop-002": "mama sola provisions"}
    out = match_shops(shops, [ZohoCustomer("5139", "Mama Sola Provisions", 500)])
    assert out.per_shop == {}
    assert set(out.ambiguous) == {"shop-001", "shop-002"}
    assert out.zoho_only == {"zoho:Mama Sola Provisions": ("Mama Sola Provisions", 500)}


def test_a_zero_balance_keeps_no_extra_row() -> None:
    out = match_shops({"shop-001": "Mama Sola Provisions"},
                      [ZohoCustomer("5139", "Somebody Else", 0)])
    assert out.per_shop == {} and out.zoho_only == {}
