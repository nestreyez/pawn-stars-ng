"""Source NG Tools : lecture de la page /hdv et passages successifs avec un faux webhook."""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from hdvbot.config import Config, WatchEntry
from hdvbot.ngtools import find_deals, parse_rows, run_ngtools


def row(id_, name, ref, ask, sold=5, listings=3):
    c = lambda v: None if v is None else round(v * 100)
    return [id_, name, c(ref), c(ask), None, None, sold, sold, 0, listings, 0, "....", 0, ""]


def page(rows):
    """Reproduit le format réel : données React découpées en plusieurs self.__next_f.push."""
    rsc = '3c:["$","$L3e",null,{"rows":' + json.dumps(rows, ensure_ascii=False) + ',"absentRows":[],"server":"ruby"}]\n'
    cut = len(rsc) // 2
    chunks = ['0:["$","html"]\n', rsc[:cut], rsc[cut:]]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(c, ensure_ascii=False)}])</script>" for c in chunks)
    return f"<html><body><main>HDV</main>{scripts}</body></html>"


BASE_ROWS = [
    row("2535:8", "Plan - Fusée spatiale", 300000, None, sold=2, listings=0),
    row("4266:0", "Carénage du Canon Zeus", 25000, 5555, sold=6, listings=9),
    row("4270:0", "Casque Survivor", 75000, 49999, sold=3, listings=7),
    row("5098:0", "Niobium", 6500, 8500, sold=45),
    row("61:0", "Fourneau", 4.69, 0.02, sold=2048),            # trop peu cher : ignoré
    row("19483:22", "Antimatter Missile", 700000, None, sold=0, listings=0),
    row("9:9", "Objet jamais vendu", 50000, 100, sold=0),       # cours non fiable : ignoré
]


def cfg_for(tmp_path, hook_url):
    return Config(server="ruby", webhook_url=hook_url, user_id=42,
                  watchlist=[WatchEntry("2535:8"), WatchEntry("4270:0", max_price=40000)],
                  db_path=str(tmp_path / "state" / "hdvbot.sqlite3"))


def test_parse_and_deals(tmp_path):
    rows = parse_rows(page(BASE_ROWS))
    assert len(rows) == 7
    r = {x.id: x for x in rows}
    assert r["4266:0"].ask == 5555 and r["4266:0"].ref == 25000 and r["4266:0"].listings == 9
    assert r["2535:8"].ask is None
    deals = find_deals(rows, cfg_for(tmp_path, ""))
    assert [d.row.name for d in deals] == ["Casque Survivor", "Carénage du Canon Zeus"]   # classé par gain/unité


def test_passages(tmp_path):
    async def scenario():
        posts = []

        async def hook(req):
            posts.append(await req.json())
            return web.json_response({"id": "1"})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        cfg = cfg_for(tmp_path, str(srv.make_url("/hook")))
        try:
            # 1er passage : "surveillance démarrée" (avec @) + récap (sans @)
            await run_ngtools(cfg, rows=parse_rows(page(BASE_ROWS)))
            assert len(posts) == 2
            assert posts[0]["content"] == "<@42>" and posts[0]["embeds"][0]["title"] == "✅ Surveillance HDV démarrée"
            assert "Plan - Fusée spatiale" in posts[0]["embeds"][0]["description"]
            recap = posts[1]
            assert "content" not in recap and recap["allowed_mentions"] == {"parse": []}
            assert recap["username"] == "Pawn Stars NG"
            d = recap["embeds"][0]["description"]
            assert d.index("1. [Casque Survivor]") < d.index("2. [Carénage du Canon Zeus]")
            assert "Fourneau" not in d and "jamais vendu" not in d

            # 2e passage : rien de neuf -> récap seul, sans @
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(BASE_ROWS)))
            assert len(posts) == 1 and "content" not in posts[0]

            # 3e passage : le Plan est mis en vente, le Casque passe à 39 000 (sous le max de 40 000) -> @
            rows3 = [list(r) for r in BASE_ROWS]
            rows3[0][3] = 25000000
            rows3[2][3] = 3900000
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(rows3)))
            assert posts[0]["content"] == "<@42>" and posts[0]["allowed_mentions"]["users"] == ["42"]
            titles = [e["title"] for e in posts[0]["embeds"]]
            assert titles == ["🔔 Plan - Fusée spatiale", "🔔 Casque Survivor"]
            assert "Mis en vente" in posts[0]["embeds"][0]["description"]
            assert "Moins cher" in posts[0]["embeds"][1]["description"]
            assert "content" not in posts[1]

            # 4e passage : mêmes prix -> pas de nouveau @
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(rows3)))
            assert len(posts) == 1 and "content" not in posts[0]
        finally:
            await srv.close()

    asyncio.run(scenario())
