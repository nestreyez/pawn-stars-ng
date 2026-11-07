"""Source NG Tools : lecture de la page /hdv et passages successifs avec un faux webhook."""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from hdvbot.config import Config, WatchEntry
from hdvbot.ngtools import find_deals, parse_rows, parse_sales, recent_reference, refresh_sales, run_ngtools


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


def money(v):
    s = f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", "\u202f").replace(".", ",")
    return s + "\u00a0$"


def item_page(sales):
    """Section « Dernières ventes » au format réel (lignes « 1 à 45 000 $ », date ISO)."""
    rows_ = []
    for k, (price, qty, iso) in enumerate(sales):
        rows_.append(["$", "div", str(k), {"className": "grid", "children": [["$", "span", None, {"className": "min-w-0", "children": [
            ["$", "span", None, {"className": "font-semibold tabular-nums", "children": str(qty)}],
            ["$", "span", None, {"className": "text-muted", "children": " à "}],
            ["$", "span", None, {"className": "font-semibold tabular-nums", "children": money(price)}],
            ["$", "span", None, {"className": "block", "children": ["achat d'une annonce", ",", " ",
                ["$", "time", None, {"dateTime": iso, "children": "il y a 1 h"}]]}]]}],
            ["$", "span", None, {"className": "text-right", "children": money(price * qty)}]]}])
    rsc = ('5:["$","section",null,{"children":[["$","h2",null,{"children":"Dernières ventes"}],'
           + json.dumps(rows_, ensure_ascii=False, separators=(',', ':')) + ']}]\n6:["$","h2",null,{"children":"Sur chaque serveur"}]\n')
    return "<html>" + "".join(f"<script>self.__next_f.push([1,{json.dumps(c, ensure_ascii=False)}])</script>"
                              for c in (rsc[:200], rsc[200:])) + "</html>"


NOW = "2026-11-07T14:03:00.000Z"
SALES = {
    # Casque : le cours 7 j dit 75 000 mais les dernières ventes tournent autour de 55 000 -> pas une affaire
    "4270:0": [(45000, 1, NOW), (75000, 1, NOW), (80000, 1, NOW), (55000, 1, NOW), (55000, 1, NOW),
               (45000, 1, NOW), (32999, 1, NOW), (30000, 1, NOW), (82000, 1, NOW), (85000, 1, NOW)],
    # Carénage : ventes ~25 000 + un transfert à 0,10 $ ignoré -> affaire à 5 555
    "4266:0": [(25000, 1, NOW), (0.10, 40, NOW), (24000, 1, NOW), (26000, 1, NOW), (25000, 2, NOW), (25500, 1, NOW)],
    # Objet rare sans cours 7 j : 3 ventes anciennes à ~300 000 -> affaire à 100 000, signalée « peu de ventes »
    "777:0": [(300000, 1, "2026-09-01T10:00:00.000Z"), (310000, 1, "2026-08-20T10:00:00.000Z"),
              (290000, 1, "2026-08-10T10:00:00.000Z")],
}

BASE_ROWS = [
    row("2535:8", "Plan - Fusée spatiale", 300000, None, sold=2, listings=0),
    row("4266:0", "Carénage du Canon Zeus", 25000, 5555, sold=6, listings=9),
    row("4270:0", "Casque Survivor", 75000, 49999, sold=3, listings=7),
    row("5098:0", "Niobium", 6500, 8500, sold=45),
    row("61:0", "Fourneau", 4.69, 0.02, sold=2048),            # trop peu cher : ignoré
    row("19483:22", "Antimatter Missile", 700000, None, sold=0, listings=0),
    row("9:9", "Objet jamais vendu", 50000, 100, sold=0),       # aucune vente connue : ignoré
    row("777:0", "Objet rare", None, 100000, sold=0, listings=1),
]


async def fake_fetch(item):
    return parse_sales(item_page(SALES.get(item, [])))


def cfg_for(tmp_path, hook_url):
    c = Config(server="ruby", webhook_url=hook_url, user_id=42,
                  watchlist=[WatchEntry("2535:8"), WatchEntry("4270:0", max_price=40000)],
                  db_path=str(tmp_path / "state" / "hdvbot.sqlite3"))
    c.ngtools["page_delay"] = 0
    return c


def test_parse_sales():
    sales = parse_sales(item_page(SALES["4266:0"]))
    assert sales[0][:2] == (25000.0, 1) and sales[1][:2] == (0.1, 40) and sales[0][2] > 0


def test_parse_and_deals(tmp_path):
    rows = parse_rows(page(BASE_ROWS))
    assert len(rows) == 8
    r = {x.id: x for x in rows}
    assert r["4266:0"].ask == 5555 and r["4266:0"].ref == 25000 and r["4266:0"].listings == 9
    assert r["2535:8"].ask is None
    cfg = cfg_for(tmp_path, "")
    ref, n = recent_reference(parse_sales(item_page(SALES["4266:0"])), cfg)
    assert ref == 25000 and n == 5                      # transfert à 0,10 $ exclu
    state = {}
    asyncio.run(refresh_sales(rows, cfg, state, fetch=fake_fetch))
    deals = find_deals(rows, cfg, state)
    # Casque exclu (49 999 vs médiane récente 55 000 = −9 %), objet rare inclus malgré 0 vente sur 7 j
    assert [d.row.name for d in deals] == ["Objet rare", "Carénage du Canon Zeus"]
    assert deals[0].n == 3


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
            await run_ngtools(cfg, rows=parse_rows(page(BASE_ROWS)), fetch_item=fake_fetch)
            assert len(posts) == 2
            assert posts[0]["content"] == "<@42>" and posts[0]["embeds"][0]["title"] == "✅ Surveillance HDV démarrée"
            assert "Plan - Fusée spatiale" in posts[0]["embeds"][0]["description"]
            recap = posts[1]
            assert "content" not in recap and recap["allowed_mentions"] == {"parse": []}
            assert recap["username"] == "Pawn Stars NG"
            d = recap["embeds"][0]["description"]
            assert d.index("1. [Objet rare]") < d.index("2. [Carénage du Canon Zeus]")
            assert "seulement 3 ventes connues" in d
            assert "Casque" not in d and "Fourneau" not in d and "jamais vendu" not in d

            # 2e passage : rien de neuf -> récap seul, sans @
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(BASE_ROWS)), fetch_item=fake_fetch)
            assert len(posts) == 1 and "content" not in posts[0]

            # 3e passage : le Plan est mis en vente, le Casque passe à 39 000 (sous le max de 40 000) -> @
            rows3 = [list(r) for r in BASE_ROWS]
            rows3[0][3] = 25000000
            rows3[2][3] = 3900000
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(rows3)), fetch_item=fake_fetch)
            assert posts[0]["content"] == "<@42>" and posts[0]["allowed_mentions"]["users"] == ["42"]
            titles = [e["title"] for e in posts[0]["embeds"]]
            assert titles == ["🔔 Plan - Fusée spatiale", "🔔 Casque Survivor"]
            assert "Mis en vente" in posts[0]["embeds"][0]["description"]
            assert "Moins cher" in posts[0]["embeds"][1]["description"]
            assert "content" not in posts[1]

            # 4e passage : mêmes prix -> pas de nouveau @
            posts.clear()
            await run_ngtools(cfg, rows=parse_rows(page(rows3)), fetch_item=fake_fetch)
            assert len(posts) == 1 and "content" not in posts[0]
        finally:
            await srv.close()

    asyncio.run(scenario())
