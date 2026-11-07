"""Test de bout en bout contre une fausse API locale (enveloppe data/meta, pagination offset)."""
import asyncio
import time
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestServer

from hdvbot.api import NGApi
from hdvbot.config import Config, WatchEntry
from hdvbot.logic import DealSettings
from hdvbot.scanner import Scanner
from hdvbot.store import Store

NOW = int(time.time())
NAMES = {"2535:8": "Rouage surveillé", "100:0": "Gros item", "200:0": "Petit item",
         "300:0": "Item au juste prix", "500:0": "Item rare"}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def make_state():
    trades = []
    def add(item, prices, qty=1, step=3600):
        for i, p in enumerate(prices):
            trades.append({"id": f"tr{len(trades)}", "item": item, "price": p, "quantity": qty,
                           "time": iso(NOW - (i + 1) * step)})
    add("100:0", [5000, 5100, 4900, 5000, 5050, 4950, 5000, 5020])
    add("100:0", [0.1] * 30, qty=10, step=1)          # transferts en rafale
    add("200:0", [50, 52, 48, 50, 51, 49])
    add("300:0", [5000, 5000, 5100, 4900, 5000])
    add("500:0", [6000, 6100])                         # trop peu de ventes -> cours API
    add("2535:8", [3200, 3100, 3300, 3150, 3250])
    trades.sort(key=lambda t: t["time"], reverse=True)
    offers = {
        "2535:8": [{"unit_price": 3000, "quantity": 1, "seller": "A"}],
        "100:0": [{"unit_price": 3000, "quantity": 2, "seller": "Bob"},
                  {"unit_price": 4900, "quantity": 1, "seller": "C"}],
        "200:0": [{"unit_price": 10, "quantity": 5, "seller": "D"}],
        "300:0": [{"unit_price": 4800, "quantity": 1, "seller": "E"}],
        "500:0": [{"unit_price": 3000, "quantity": 1, "seller": "F"}],
    }
    api_ref = {"500:0": 6000}
    return {"trades": trades, "offers": offers, "api_ref": api_ref, "calls": []}


def make_app(st):
    def page(rows, req):
        limit = int(req.query.get("limit", 100))
        off = int(req.query.get("offset", 0))
        return web.json_response({"data": rows[off:off + limit],
                                  "meta": {"total": len(rows), "stale": False, "domain": "hdv_book"}})

    async def items(req):
        st["calls"].append("items")
        rows = []
        for item, name in NAMES.items():
            iid, meta = item.split(":")
            offs = st["offers"].get(item, [])
            rows.append({"item": {"id": int(iid), "meta": int(meta), "name": {"fr": name}},
                         "book": {"lowest_price": min((o["unit_price"] for o in offs), default=None),
                                  "count": len(offs)},
                         "trade": {"median_7d": st["api_ref"].get(item)}})
        return page(rows, req)

    async def item(req):
        iid = req.match_info["item"]
        st["calls"].append(f"item:{iid}")
        if iid not in NAMES:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"data": {"id": iid, "name": NAMES[iid], "listings": st["offers"].get(iid, []),
                                           "recent_trades": [t for t in st["trades"] if t["item"] == iid][:5]}})

    async def trades(req):
        it = req.query.get("item")
        st["calls"].append(f"trades:{it}")
        rows = [t for t in st["trades"] if it is None or t["item"] == it]
        return page(rows, req)

    app = web.Application()
    app.router.add_get("/v1/servers/ruby/hdv/items", items)
    app.router.add_get("/v1/servers/ruby/hdv/items/{item}", item)
    app.router.add_get("/v1/servers/ruby/hdv/trades", trades)
    return app


def test_full_cycles(tmp_path):
    async def scenario():
        st = make_state()
        server = TestServer(make_app(st))
        await server.start_server()
        try:
            cfg = Config(server="ruby", base_url=str(server.make_url("/v1")), request_delay=0, page_size=7,
                         watchlist=[WatchEntry("2535:8")], db_path=str(tmp_path / "db.sqlite3"),
                         deals=DealSettings(min_value=2000, min_discount=0.3))
            store = Store(cfg.db_path)
            async with NGApi(cfg.base_url, cfg.server, None, 0, cfg.page_size) as api:
                sc = Scanner(cfg, api, store)

                # Passage 1 : watchlist initialisée en silence, affaires détectées
                hits, deals = await sc.cycle()
                assert hits == []
                by_item = {d.item: d for d in deals}
                assert set(by_item) == {"100:0", "500:0"}, by_item
                big = by_item["100:0"]
                assert big.name == "Gros item" and big.ask == 3000 and 4900 <= big.ref.price <= 5100
                assert big.ref.source == "ventes" and big.cheap_qty == 2
                assert by_item["500:0"].ref.source == "api"
                assert store.recent_trades("100:0")            # flux global stocké (pagination OK)

                # Passage 2 : nouvelle annonce moins chère sur l'item surveillé, pas de re-ping des affaires
                st["offers"]["2535:8"].append({"unit_price": 2500, "quantity": 3, "seller": "Z"})
                hits, deals = await sc.cycle()
                assert [h.reason for h in hits] == ["nouvelle annonce moins chère"]
                assert hits[0].ask == 2500 and hits[0].ask_qty == 3 and hits[0].name == "Rouage surveillé"
                assert deals == []

                # Passage 3 : l'affaire baisse encore -> re-ping ; l'item rare part -> ré-armé
                st["offers"]["100:0"][0]["unit_price"] = 2500
                st["offers"]["500:0"] = []
                hits, deals = await sc.cycle()
                assert hits == [] and [d.item for d in deals] == ["100:0"]
                assert store.get_state("deal", "500:0") is None

                # Passage 4 : l'item rare revient en affaire -> nouveau ping
                st["offers"]["500:0"] = [{"unit_price": 2000, "quantity": 1, "seller": "F"}]
                hits, deals = await sc.cycle()
                assert [d.item for d in deals] == ["500:0"]

                # Recherche manuelle (/prix)
                snap, ref, trades = await sc.lookup("100:0")
                assert snap.ask == 2500 and ref.source == "ventes" and trades
        finally:
            await server.close()

    asyncio.run(scenario())
