"""Normalisation des réponses de l'API NationsGlory.

La doc publique ne détaille pas tous les noms de champs, donc on reconnaît
plusieurs variantes courantes. Si un champ est mal détecté, `python -m hdvbot probe`
montre le JSON brut et la section `fields:` du config.yaml permet de forcer
le bon chemin (ex. `item.ask: "book.best_ask"`).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- Noms de champs reconnus automatiquement -------------------------------
ITEM_ID_KEYS = ["id", "item_id", "itemId", "item", "key", "uid", "ref"]
META_KEYS = ["meta", "damage", "variant", "data", "dv", "metadata"]
NAME_KEYS = ["name", "display_name", "displayName", "label", "item_name", "itemName",
             "title", "localized_name", "nom", "name_fr", "fr"]
ASK_KEYS = ["best_ask", "bestAsk", "lowest_ask", "lowestAsk", "lowest_price", "lowestPrice",
            "min_price", "minPrice", "price_min", "min_ask", "ask", "cheapest",
            "cheapest_price", "floor", "floor_price", "best_price", "sell_price"]
ASK_QTY_KEYS = ["best_ask_quantity", "best_ask_qty", "ask_quantity", "ask_qty",
                "lowest_quantity", "cheapest_quantity"]
REF_KEYS = ["price_7d", "avg_price_7d", "average_price_7d", "median_price_7d", "median_7d",
            "vwap_7d", "course_7d", "cours_7d", "course", "cours", "median_price", "median",
            "avg_price", "average_price", "price_avg", "vwap", "reference_price", "market_price"]
COUNT_KEYS = ["listings_count", "listing_count", "offers_count", "for_sale", "on_sale",
              "quantity_for_sale", "supply", "stock", "listings", "offers", "count"]
NESTED_KEYS = ["book", "hdv_book", "market", "stats", "statistics", "prices", "price",
               "trade", "hdv_trade", "summary", "item", "info"]
OFFERS_KEYS = ["listings", "asks", "offers", "sell_orders", "sells", "orders", "entries",
               "book", "en_vente"]
TRADES_KEYS = ["trades", "recent_trades", "last_trades", "sales", "history", "transactions"]
UNIT_PRICE_KEYS = ["unit_price", "unitPrice", "price_per_unit", "price_unit", "pu",
                   "prix_unitaire"]
PRICE_KEYS = ["price", "prix"]
TOTAL_KEYS = ["total", "total_price", "totalPrice", "amount_paid", "value"]
QTY_KEYS = ["quantity", "qty", "amount", "count", "stack", "size", "quantite"]
SELLER_KEYS = ["seller", "seller_name", "player", "owner", "username", "user", "vendeur"]
TS_KEYS = ["timestamp", "time", "date", "created_at", "createdAt", "sold_at", "traded_at",
           "at", "ts", "datetime"]
TRADE_ID_KEYS = ["id", "trade_id", "uid", "uuid"]

_ID_RE = re.compile(r"^(\d+)\s*[:\-_/]\s*(\d+)$")
_TRAILING_ID_RE = re.compile(r"(\d+)[-:](\d+)/?$")


@dataclass
class Offer:
    price: float          # prix unitaire
    qty: int
    seller: str | None = None

    @property
    def signature(self) -> str:
        return f"{self.price:.4f}|{self.qty}|{self.seller or ''}"


@dataclass
class Trade:
    uid: str
    item: str
    price: float          # prix unitaire
    qty: int
    ts: int               # epoch secondes


@dataclass
class ItemSnap:
    id: str                       # format API "264:0"
    name: str | None = None
    ask: float | None = None      # prix unitaire le plus bas en vente
    ask_qty: int | None = None
    count: int | None = None      # nb d'annonces / quantité en vente
    api_ref: float | None = None  # prix de référence fourni par l'API, si présent
    offers: list[Offer] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return self.name or self.id


# --- Identifiants d'items ---------------------------------------------------
def parse_item_ref(text: str) -> str:
    """'2535-8', '2535:8', '2535', une URL nationsglory.fr ou ng-tools.fr -> '2535:8'."""
    s = str(text).strip().split("?")[0].split("#")[0].rstrip("/")
    m = _ID_RE.match(s)
    if m:
        return f"{int(m.group(1))}:{int(m.group(2))}"
    if s.isdigit():
        return f"{int(s)}:0"
    m = _TRAILING_ID_RE.search(s)
    if m:
        return f"{int(m.group(1))}:{int(m.group(2))}"
    raise ValueError(f"Identifiant d'item non reconnu : {text!r} (attendu ex. 2535-8)")


def web_id(item_id: str) -> str:
    """'2535:8' -> '2535-8' (format des URLs du site)."""
    return item_id.replace(":", "-")


# --- Petits utilitaires ------------------------------------------------------
def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def to_num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(" ", "").replace("\xa0", "").replace(" ", "").replace("$", "")
        t = t.replace(",", ".") if t.count(",") == 1 and "." not in t else t.replace(",", "")
        try:
            return float(t)
        except ValueError:
            return None
    return None


def to_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        for k in ("fr", "name", "username", "display_name", "label"):
            if isinstance(v.get(k), str):
                return v[k]
        return None
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def to_ts(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return int(x / 1000) if x > 1e12 else int(x)
    if isinstance(v, str):
        s = v.strip()
        if s.replace(".", "", 1).isdigit():
            return to_ts(float(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


ASK_NESTED = ["book", "hdv_book", "market", "asks", "ask", "sell", "prices", "price", "summary"]
REF_NESTED = ["trade", "hdv_trade", "stats", "statistics", "prices", "price", "summary", "market"]


def _first(rec: dict, keys: list[str], conv, nested: bool | list[str] = True):
    for k in keys:
        if k in rec:
            v = conv(rec[k])
            if v is not None:
                return v
    if nested:
        for nk in (nested if isinstance(nested, list) else NESTED_KEYS):
            sub = rec.get(nk)
            if isinstance(sub, dict):
                v = _first(sub, keys, conv, nested=False)
                if v is not None:
                    return v
    return None


def _first_list(rec: dict, keys: list[str]) -> list[dict] | None:
    """Cherche une liste d'objets sous l'une des clés (y compris un niveau plus bas)."""
    for k in keys:
        v = rec.get(k)
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
        if isinstance(v, list) and not v:
            return []
    for nk in NESTED_KEYS + keys:
        sub = rec.get(nk)
        if isinstance(sub, dict):
            for k in keys:
                v = sub.get(k)
                if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                    return v
    return None


def _override(ov: dict, key: str, rec: dict, conv):
    path = ov.get(key)
    if not path:
        return None, False
    return conv(get_path(rec, path)), True


# --- Identifiant d'un enregistrement ----------------------------------------
def record_item_id(rec: dict, ov: dict | None = None) -> str | None:
    ov = ov or {}
    if ov.get("item.id"):
        v = get_path(rec, ov["item.id"])
        return _id_from_value(v, rec)
    # 1er passage : identifiants explicites "264:0" ou objet item imbriqué
    for k in ITEM_ID_KEYS:
        v = rec.get(k)
        if isinstance(v, dict):
            sub = record_item_id(v)
            if sub:
                return sub
        elif isinstance(v, str) and _ID_RE.match(v.strip()):
            return parse_item_ref(v)
    # 2e passage : id numérique (+ meta éventuel)
    for k in ITEM_ID_KEYS:
        got = _id_from_value(rec.get(k), rec) if not isinstance(rec.get(k), dict) else None
        if got:
            return got
    return None


def _id_from_value(v: Any, rec: dict) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            return parse_item_ref(v)
        except ValueError:
            return None
    if isinstance(v, (int, float)):
        meta = 0
        for mk in META_KEYS:
            m = rec.get(mk)
            if isinstance(m, (int, float)) and not isinstance(m, bool):
                meta = int(m)
                break
        return f"{int(v)}:{meta}"
    return None


# --- Annonces / ventes -------------------------------------------------------
def _unit_price(rec: dict, ov: dict, prefix: str) -> tuple[float | None, int]:
    qty_v, forced = _override(ov, f"{prefix}.qty", rec, to_num)
    qty = qty_v if forced else _first(rec, QTY_KEYS, to_num, nested=False)
    qty_i = int(qty) if qty and qty > 0 else 1

    unit, forced = _override(ov, f"{prefix}.price", rec, to_num)
    if forced and unit is not None:
        if ov.get(f"{prefix}.price_is_total"):
            unit = unit / qty_i
        return unit, qty_i

    unit = _first(rec, UNIT_PRICE_KEYS, to_num, nested=False)
    if unit is not None:
        return unit, qty_i
    price = _first(rec, PRICE_KEYS, to_num, nested=False)
    total = _first(rec, TOTAL_KEYS, to_num, nested=False)
    if price is not None and total is not None and qty_i > 1:
        # Si price ≈ total, c'est le prix du lot.
        if abs(price - total) <= 1e-6 * max(1.0, total):
            return total / qty_i, qty_i
        return price, qty_i
    if price is not None:
        return (price / qty_i if ov.get(f"{prefix}.price_is_total") else price), qty_i
    if total is not None:
        return total / qty_i, qty_i
    return None, qty_i


def normalize_offer(rec: dict, ov: dict | None = None) -> Offer | None:
    ov = ov or {}
    price, qty = _unit_price(rec, ov, "offer")
    if price is None or price <= 0:
        return None
    seller, forced = _override(ov, "offer.seller", rec, to_text)
    if not forced:
        seller = _first(rec, SELLER_KEYS, to_text, nested=False)
    return Offer(price=price, qty=qty, seller=seller)


def normalize_trade(rec: dict, item_hint: str | None = None, ov: dict | None = None) -> Trade | None:
    ov = ov or {}
    price, qty = _unit_price(rec, ov, "trade")
    if price is None or price <= 0:
        return None
    item = None
    if ov.get("trade.item"):
        item = _id_from_value(get_path(rec, ov["trade.item"]), rec)
    if item is None:
        for k in ("item", "item_id", "itemId"):
            if k in rec:
                v = rec[k]
                item = record_item_id(v) if isinstance(v, dict) else _id_from_value(v, rec)
                if item:
                    break
    item = item or item_hint
    if item is None:
        return None
    ts, forced = _override(ov, "trade.ts", rec, to_ts)
    if not forced:
        ts = _first(rec, TS_KEYS, to_ts, nested=False)
    ts = ts or int(datetime.now(timezone.utc).timestamp())
    uid = None
    for k in TRADE_ID_KEYS:
        if k in rec and isinstance(rec[k], (str, int)) and not isinstance(rec[k], bool):
            # 'id' peut être l'id de l'item dans certains formats : on ne le garde que s'il diffère
            cand = str(rec[k])
            if k == "id" and _ID_RE.match(cand):
                continue
            uid = f"{k}:{cand}"
            break
    if uid is None:
        uid = "h:" + hashlib.sha1(f"{item}|{ts}|{price:.6f}|{qty}".encode()).hexdigest()[:20]
    return Trade(uid=uid, item=item, price=price, qty=qty, ts=ts)


# --- Item complet -----------------------------------------------------------
def normalize_item(rec: dict, ov: dict | None = None, item_hint: str | None = None) -> ItemSnap | None:
    ov = ov or {}
    item_id = record_item_id(rec, ov) or item_hint
    if item_id is None:
        return None

    name, forced = _override(ov, "item.name", rec, to_text)
    if not forced:
        name = _first(rec, NAME_KEYS, to_text)
    if name and _ID_RE.match(name):
        name = None

    offers_raw = get_path(rec, ov["item.offers"]) if ov.get("item.offers") else _first_list(rec, OFFERS_KEYS)
    offers = [o for o in (normalize_offer(x, ov) for x in (offers_raw or []) if isinstance(x, dict)) if o]
    offers.sort(key=lambda o: o.price)

    trades_raw = get_path(rec, ov["item.trades"]) if ov.get("item.trades") else _first_list(rec, TRADES_KEYS)
    trades = [t for t in (normalize_trade(x, item_id, ov) for x in (trades_raw or []) if isinstance(x, dict)) if t]

    ask, forced = _override(ov, "item.ask", rec, to_num)
    if not forced:
        ask = _first(rec, ASK_KEYS, to_num, nested=ASK_NESTED)
    if offers and (ask is None or offers[0].price < ask):
        ask = offers[0].price
    if ask is not None and ask <= 0:
        ask = None

    ask_qty = _first(rec, ASK_QTY_KEYS, to_num, nested=ASK_NESTED)
    if ask_qty is None and offers:
        ask_qty = offers[0].qty

    count, forced = _override(ov, "item.count", rec, to_num)
    if not forced:
        count = _first(rec, COUNT_KEYS, to_num, nested=ASK_NESTED)
    if count is None and offers_raw is not None:
        count = len(offers)

    api_ref, forced = _override(ov, "item.ref", rec, to_num)
    if not forced:
        api_ref = _first(rec, REF_KEYS, to_num, nested=REF_NESTED)

    return ItemSnap(
        id=item_id, name=name, ask=ask,
        ask_qty=int(ask_qty) if ask_qty else None,
        count=int(count) if count is not None else None,
        api_ref=api_ref if api_ref and api_ref > 0 else None,
        offers=offers, trades=trades, raw=rec,
    )
