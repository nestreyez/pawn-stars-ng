"""Mode GitHub Actions : passages successifs avec état persistant, envoi par (faux) webhook Discord."""
import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from hdvbot.action import run_action
from hdvbot.config import Config, WatchEntry
from hdvbot.logic import DealSettings
from test_e2e import make_app, make_state


def test_action_runs(tmp_path, monkeypatch):
    async def scenario():
        st = make_state()
        posts = []
        limited = []

        async def hook(req):
            assert req.query.get("wait") == "true"
            if not limited:              # simule une limitation Discord une fois
                limited.append(1)
                return web.json_response({"retry_after": 0.05}, status=429)
            posts.append(await req.json())
            return web.json_response({"id": "1"})

        hook_app = web.Application()
        hook_app.router.add_post("/api/webhooks/1/abc", hook)
        api_srv, hook_srv = TestServer(make_app(st)), TestServer(hook_app)
        await api_srv.start_server()
        await hook_srv.start_server()

        def cfg(watch):
            return Config(server="ruby", base_url=str(api_srv.make_url("/v1")), request_delay=0, page_size=50,
                          webhook_url=str(hook_srv.make_url("/api/webhooks/1/abc")), user_id=42,
                          watchlist=[WatchEntry(w) for w in watch], db_path=str(tmp_path / "state" / "db.sqlite3"),
                          deals=DealSettings(min_value=2000, min_discount=0.3))
        (tmp_path / "state").mkdir()
        try:
            # 1er passage : message "surveillance démarrée" + affaires actuelles
            assert await run_action(cfg(["2535:8", "200:0"])) == 0
            assert posts[0]["embeds"][0]["title"] == "✅ Surveillance HDV démarrée"
            assert posts[0]["content"] == "<@42>" and posts[0]["allowed_mentions"]["users"] == ["42"]
            assert "Rouage surveillé" in posts[0]["embeds"][0]["description"]
            # puis le récap sans ping des affaires (pas de ping par affaire)
            assert len(posts) == 2
            recap = posts[1]
            assert recap.get("content") is None and recap["allowed_mentions"] == {"parse": []}
            assert recap["embeds"][0]["title"] == "📊 Meilleures affaires HDV Ruby"
            d = recap["embeds"][0]["description"]
            assert "1. [Gros item]" in d and "2. [Item rare]" in d
            assert recap["username"] == "HDV Ruby"

            # 2e passage : rien de neuf -> juste le récap, sans ping
            posts.clear()
            await run_action(cfg(["2535:8", "200:0"]))
            assert len(posts) == 1 and posts[0]["embeds"][0]["title"].startswith("📊")
            assert posts[0].get("content") is None

            # 3e passage : nouvelle annonce moins chère + 200:0 retiré de config.yaml + dépôt inactif depuis 55 j
            st["offers"]["2535:8"].append({"unit_price": 2500, "quantity": 3, "seller": "Z"})
            monkeypatch.setenv("HDVBOT_LAST_COMMIT_TS", str(int(time.time()) - 55 * 86400))
            await run_action(cfg(["2535:8"]))
            titles = [e.get("title") for p in posts for e in p["embeds"]]
            assert "🔔 Rouage surveillé" in titles
            ping_msgs = [p for p in posts if p.get("content")]
            assert ping_msgs and all("🔔" in p["embeds"][0].get("title", "") or "⏰" in p["embeds"][0]["description"] for p in ping_msgs)
            assert any("Aucun commit sur le dépôt depuis 55 jours" in e["description"] for p in posts for e in p["embeds"])
            from hdvbot.store import Store
            assert [w["item"] for w in Store(str(tmp_path / "state" / "db.sqlite3")).watch_list()] == ["2535:8"]

            # 4e passage : rappel d'inactivité pas répété le même jour (seulement le récap)
            posts.clear()
            await run_action(cfg(["2535:8"]))
            assert len(posts) == 1 and posts[0]["embeds"][0]["title"].startswith("📊")
        finally:
            await api_srv.close()
            await hook_srv.close()

    asyncio.run(scenario())


def test_api_down_warning(tmp_path):
    async def scenario():
        posts = []

        async def hook(req):
            posts.append(await req.json())
            return web.json_response({"id": "1"})

        hook_app = web.Application()
        hook_app.router.add_post("/hook", hook)
        down = web.Application()                    # API qui répond 503 partout
        async def unavailable(_):
            return web.Response(status=503)
        down.router.add_get("/{tail:.*}", unavailable)
        hs, ds = TestServer(hook_app), TestServer(down)
        await hs.start_server()
        await ds.start_server()
        cfg = Config(server="ruby", base_url=str(ds.make_url("/v1")), request_delay=0, user_id=42,
                     webhook_url=str(hs.make_url("/hook")), db_path=str(tmp_path / "db.sqlite3"))
        import hdvbot.api as api_mod
        orig = asyncio.sleep
        api_mod.asyncio.sleep = lambda *_: orig(0)   # pas d'attente entre les tentatives
        try:
            for _ in range(4):
                await run_action(cfg)
            assert len(posts) == 1 and "ne répond plus depuis 3 passages" in posts[0]["embeds"][0]["description"]
        finally:
            api_mod.asyncio.sleep = orig
            await hs.close()
            await ds.close()

    asyncio.run(scenario())
