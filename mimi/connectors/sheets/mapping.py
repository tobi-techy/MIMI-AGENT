"""Column mapping and SKU matching for sheet and CSV rows (RAI-277).

The model suggests the mapping; the shop confirms it before it is saved
(a mapping can never be saved unconfirmed). Item names match the
distributor catalogue by code first (exact, then normalised), with fuzzy
match plus confirm for low confidence. Zero numbers invented or changed:
quantities and dates come from the row or the mapping stays empty.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

# Fixed shop phrases mapped without a model call (English, Pidgin, Yoruba,
# Hausa, Igbo): the suggestion still waits for the shop's confirm.
HEADER_SYNONYMS: dict[str, str] = {
    "item": "item", "product": "item", "goods": "item", "sku": "sku",
    "qty left": "qty_left", "qty": "qty_left", "quantity": "qty_left",
    "stock left": "qty_left", "stock": "qty_left", "balance": "qty_left",
    "remaining": "qty_left", "left": "qty_left",
    "date": "date", "day": "date", "as of": "date", "as_of": "date",
    "price": "skip", "amount": "skip", "total": "skip", "note": "skip",
    "remark": "skip", "supplier": "skip", "brand": "skip",
    # Yoruba (ASCII renderings shops type): oja (goods), elo ku (how many
    # remain), ojo (day).
    "oja": "item", "elo ku": "qty_left", "ojo": "date",
    # Hausa: kaya (goods), adadi (number), rana (day).
    "kaya": "item", "adadi": "qty_left", "rana": "date",
    # Igbo (ASCII renderings): ngwa ahia (goods), ihe foduru (what remains),
    # ubochi (day).
    "ngwa ahia": "item", "ihe foduru": "qty_left", "ubochi": "date",
}

# Canonical words for the typo fallback: a header close to one of these
# (typos like "Itme") maps the same way. Checked only when no rule hits.
_FUZZY_CANONICAL: dict[str, str] = {
    "item": "item", "product": "item", "goods": "item", "quantity": "qty_left",
    "qty": "qty_left", "stock": "qty_left", "balance": "qty_left",
    "remaining": "qty_left", "date": "date", "day": "date",
}

MAX_ROWS = 10_000


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def suggest_mapping(header: list[str]) -> dict[str, str]:
    """First suggestion for ``Item to Product, Qty left to Stock left,
    Date to Date, others Skip``. The shop confirms before it is saved."""
    out: dict[str, str] = {}
    for col in header:
        key = _norm(col)
        if key in HEADER_SYNONYMS:
            out[str(col)] = HEADER_SYNONYMS[key]
            continue
        if any(k in key for k in ("item", "product", "goods", "name", "sku")):
            out[str(col)] = "item"
        elif any(k in key for k in ("date", "day", "as of", "when")):
            out[str(col)] = "date"
        elif any(k in key for k in ("qty", "quant", "stock", "balance", "remain",
                                    "count", "pcs", "ctn", "carton")):
            # "remain" alone (a prose note like "wetin remain note") is not a
            # quantity column: it needs "qty", "left", "stock" or a unit word
            # beside it before it counts.
            if "remain" in key and not any(
                    w in key for w in ("qty", "quant", "stock", "balance",
                                       "left", "count", "pcs", "ctn", "carton")):
                out[str(col)] = _fuzzy(key)
            else:
                out[str(col)] = "qty_left"
        elif "left" in key and not any(
                w in key for w in ("qty", "quant", "stock", "balance", "remain",
                                   "count", "pcs", "ctn", "carton")):
            out[str(col)] = _fuzzy(key)
        else:
            out[str(col)] = _fuzzy(key)
    return out


def _fuzzy(key: str) -> str:
    """A typo'd header ("Itme") maps like its canonical word; else Skip."""
    hit = difflib.get_close_matches(key, list(_FUZZY_CANONICAL), n=1, cutoff=0.7)
    if hit:
        return _FUZZY_CANONICAL[hit[0]]
    return "skip"


def map_rows(grid: list[list[Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Map a values grid (first row header) into book rows. Raises
    ``ValueError`` with a plain message for bad files."""
    if not grid or not any(any(str(c).strip() for c in r) for r in grid):
        raise ValueError("That file is empty. Send a sheet with an item column.")
    header = [str(c).strip() for c in grid[0]]
    body = [r for r in grid[1:] if any(str(c).strip() for c in r)]
    if len(body) > MAX_ROWS:
        raise ValueError("That file is too big (over 10,000 rows). Send a smaller one.")
    if not mapping:
        mapping = suggest_mapping(header)
    # A SKU column is the item identifier: it counts as the item column.
    item_col = next((i for i, c in enumerate(header)
                     if mapping.get(c) in ("item", "sku")), None)
    if item_col is None:
        raise ValueError("I cannot see an item column. Fix the mapping and confirm.")
    qty_col = next((i for i, c in enumerate(header) if mapping.get(c) == "qty_left"), None)
    date_col = next((i for i, c in enumerate(header) if mapping.get(c) == "date"), None)
    out: list[dict[str, Any]] = []
    for r in body:
        item = str(r[item_col]).strip() if item_col < len(r) else ""
        if not item:
            continue
        row: dict[str, Any] = {"item": item}
        if qty_col is not None and qty_col < len(r):
            row["qty_left"] = _quantity(r[qty_col])
        if date_col is not None and date_col < len(r):
            row["date"] = str(r[date_col]).strip()
        out.append(row)
    return out


def _quantity(raw: Any) -> int | None:
    """A cell into a whole-unit quantity. None when it is not a number:
    never guessed, never invented."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(str(raw).replace(",", "").strip()))
    except ValueError:
        return None


def match_sku(name: str, catalogue: list[dict[str, Any]]) -> dict[str, Any]:
    """Match one item name to the distributor catalogue.

    Exact match on code or normalised name first; fuzzy match above 0.8
    asks the shop to confirm (``needs_confirm``); below that it stays
    unmatched. Returns ``{"sku": ..., "confidence": ..., "needs_confirm"}``.
    """
    want = _norm(name)
    if not want:
        return {"sku": None, "confidence": 0.0, "needs_confirm": True}
    for item in catalogue:
        if _norm(item.get("sku")) == want or _norm(item.get("name")) == want:
            return {"sku": item.get("sku"), "confidence": 1.0, "needs_confirm": False}
    names = [_norm(i.get("name")) for i in catalogue]
    hit = difflib.get_close_matches(want, names, n=1, cutoff=0.6)
    if not hit:
        return {"sku": None, "confidence": 0.0, "needs_confirm": True}
    score = difflib.SequenceMatcher(None, want, hit[0]).ratio()
    sku = next(i.get("sku") for i in catalogue if _norm(i.get("name")) == hit[0])
    if score >= 0.8:
        return {"sku": sku, "confidence": round(score, 2), "needs_confirm": True}
    return {"sku": None, "confidence": round(score, 2), "needs_confirm": True}
