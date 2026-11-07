import time
from datetime import datetime, timezone

import pytest

from hdvbot.fields import (ItemSnap, Offer, Trade, normalize_item, normalize_trade, parse_item_ref,
                           record_item_id)
from hdvbot.logic import (DealSettings, Reference, deal_gate, evaluate_deal, fmt_money,
                          reference_from_trades, watch_gate)

NOW = int(time.time())


# --- identifiants ---------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("2535-8", "2535:8"), ("2535:8", "2535:8"), ("5097", "5097:0"),
    ("https://nationsglory.fr/servers/ruby/hdv/10162-0", "10162:0"),
    ("https://ng-tools.fr/hdv/ruby/rouage-en-adamantium-4510-6", "4510:6"),
    ("  264:0 ", "264:0"),
])
def test_parse_item_ref(raw, expected):
    assert parse_item_ref(raw) == expected


def test_parse_item_ref_invalid():
    with pytest.raises(ValueError):
        parse_item_ref("épée en diamant")


def test_record_id_prefers_item_over_db_id():
    assert record_item_id({"id": 4512, "item": "264:0"}) == "264:0"
    assert record_item_id({"item": {"id": 2535, "meta": 8}}) == "2535:8"
    assert record_item_id({"id": 2535, "damage": 8}) == "2535:8"


# --- normalisation de formats variés ---------------------------------------------------
def test_normalize_flat_item():
    s = normalize_item({"id": "2535:8", "name": "Rouage", "best_ask": 120.5, "listings_count": 4, "price_7d": 150})
    assert (s.id, s.name, s.ask, s.count, s.api_ref) == ("2535:8", "Rouage", 120.5, 4, 150)


def test_normalize_nested_item():
    s = normalize_item({"item": {"id": 4510, "meta": 6, "name": {"fr": "Rouage en adamantium"}},
                        "book": {"lowest_price": 59.38, "count": 160},
                        "trade": {"median_7d": 59.30, "min_price": 0.1}})
    assert s.id == "4510:6" and s.name == "Rouage en adamantium"
    assert s.ask == 59.38 and s.count == 160 and s.api_ref == 59.30   # min_price des ventes ignoré


def test_normalize_detail_with_offers_total_price():
    s = normalize_item({"id": "100:0", "name": "X",
                        "listings": [{"price": 2000, "total": 2000, "quantity": 32, "seller": "Bob"},
                                     {"unit_price": 50, "quantity": 2, "seller": {"username": "Al"}}]})
    assert [round(o.price, 2) for o in s.offers] == [50, 62.5]   # 2000/32 = lot entier détecté
    assert s.ask == 50 and s.ask_qty == 2 and s.offers[0].seller == "Al" and s.count == 2


def test_normalize_trade_formats():
    expected = int(datetime(2026, 11, 7, 10, tzinfo=timezone.utc).timestamp())
    t = normalize_trade({"item": "264:0", "unit_price": "1 250,5", "quantity": 3, "time": "2026-11-07T10:00:00Z"})
    assert t.item == "264:0" and t.price == 1250.5 and t.qty == 3 and t.ts == expected
    t2 = normalize_trade({"price": 62.5, "total": 2000, "qty": 32, "timestamp": expected * 1000}, item_hint="1:0")
    assert t2.price == 62.5 and t2.ts == expected
    assert t2.uid.startswith("h:")


def test_overrides():
    s = normalize_item({"key": "1:0", "weird": {"floor$": 42}}, {"item.ask": "weird.floor$"})
    assert s.ask == 42


# --- prix de référence --------------------------------------------------------------------
def trades(prices, item="1:0", qty=1, start=NOW):
    return [Trade(uid=f"t{i}", item=item, price=p, qty=qty, ts=start - i * 3600) for i, p in enumerate(prices)]


def test_reference_ignores_repeated_transfers():
    s = DealSettings()
    real = trades([5000, 5100, 4900, 5050, 4950, 5000], qty=1)
    transfers = [Trade(uid=f"x{i}", item="1:0", price=0.1, qty=10, ts=NOW - 60) for i in range(40)]
    ref = reference_from_trades(real + transfers, s)
    assert ref is not None and 4900 <= ref.price <= 5100 and ref.n == 6


def test_reference_ignores_money_transfer_spike():
    s = DealSettings()
    ref = reference_from_trades(trades([5000, 5000, 1_000_000, 4900, 5100, 5000]), s)
    assert ref.price == 5000


# --- bonnes affaires ---------------------------------------------------------------------------
def test_evaluate_deal():
    s = DealSettings(min_value=2000, min_discount=0.3)
    ref = Reference(5000, 10, "ventes")
    snap = ItemSnap(id="1:0", ask=3000, offers=[Offer(3000, 2), Offer(3400, 1), Offer(4000, 5)])
    d = evaluate_deal(snap, ref, s)
    assert d and round(d.discount, 2) == 0.4
    assert d.cheap_qty == 3 and d.profit == 2 * 2000 + 1 * 1600
    assert evaluate_deal(ItemSnap(id="1:0", ask=3600), ref, s) is None               # décote 28 %
    assert evaluate_deal(ItemSnap(id="1:0", ask=10), Reference(1500, 10, "ventes"), s) is None  # < 2k


def test_deal_gate():
    ok, st = deal_gate(None, 3000, 0.05)
    assert ok
    ok, st = deal_gate(st, 3000, 0.05)
    assert not ok                       # même annonce : pas de re-ping
    ok, st = deal_gate(st, 2900, 0.05)
    assert not ok                       # -3 % seulement
    ok, st = deal_gate(st, 2700, 0.05)
    assert ok                           # nouvelle baisse nette


# --- watchlist -----------------------------------------------------------------------------------
def test_watch_gate_cheaper():
    snap = lambda ask, offers=(): ItemSnap(id="1:0", ask=ask, count=1 if ask else 0, offers=list(offers))
    reason, _, st = watch_gate(None, snap(3000), None)
    assert reason is None                                        # 1er passage : silencieux
    reason, _, st = watch_gate(st, snap(3000), None)
    assert reason is None
    reason, _, st = watch_gate(st, snap(2500), None)
    assert reason == "nouvelle annonce moins chère"
    reason, _, st = watch_gate(st, snap(None), None)
    assert reason is None and st["last_ask"] is None             # rupture
    reason, _, st = watch_gate(st, snap(2800), None)
    assert reason == "de nouveau en vente"
    reason, _, st = watch_gate(st, snap(2600), 2000)
    assert reason is None                                        # au-dessus du prix max
    reason, _, st = watch_gate(st, snap(1900), 2000)
    assert reason == "nouvelle annonce moins chère"


def test_watch_gate_all_mode():
    s1 = ItemSnap(id="1:0", ask=100, offers=[Offer(100, 1, "A")])
    _, _, st = watch_gate(None, s1, None, "all")
    s2 = ItemSnap(id="1:0", ask=100, offers=[Offer(100, 1, "A"), Offer(150, 4, "B")])
    reason, fresh, st = watch_gate(st, s2, None, "all")
    assert reason == "nouvelle annonce" and fresh == [Offer(150, 4, "B")]


def test_fmt_money():
    assert fmt_money(2000) == "2 000 $"
    assert fmt_money(62.5) == "62,50 $"
    assert fmt_money(None) == "—"
