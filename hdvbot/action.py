"""Mode GitHub Actions : un passage de surveillance, alertes envoyées par webhook Discord.

Lancé toutes les ~15 min par .github/workflows/hdv.yml. L'historique (SQLite) est gardé
entre deux passages grâce au cache GitHub (dossier state/).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

from .api import NGApi
from .config import Config
from .logic import fmt_money
from .render import BLUE, ORANGE, alert_payloads, summary_payload, text_payload
from .scanner import Scanner
from .store import Store

log = logging.getLogger("hdvbot.action")

INACTIVITY_WARN_DAYS = 50     # GitHub suspend les tâches planifiées d'un dépôt public après 60 jours sans commit
FAILURES_BEFORE_WARNING = 3


class WebhookError(Exception):
    pass


def _webhook_url(url: str) -> str:
    return url if "wait=" in url else url + ("&" if "?" in url else "?") + "wait=true"


async def send_webhook(url: str, payloads: list[dict], username: str | None = None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        for p in payloads:
            body = {k: v for k, v in p.items() if v is not None}
            if username:
                body["username"] = username
            for _ in range(5):
                async with s.post(_webhook_url(url), json=body) as r:
                    if r.status == 429:
                        data = await r.json(content_type=None) or {}
                        await asyncio.sleep(float(data.get("retry_after", 2)) + 0.5)
                        continue
                    if r.status >= 400:
                        raise WebhookError(f"Webhook Discord refusé ({r.status}) : {(await r.text())[:300]}")
                    break
            else:
                raise WebhookError("Webhook Discord : trop de limitations (429)")
            await asyncio.sleep(0.4)


def _watch_lines(cfg: Config, store: Store) -> list[str]:
    lines = []
    for w in store.watch_list():
        st = store.get_state("watch", w["item"]) or {}
        name = store.get_name(w["item"]) or w["item"].replace(":", "-")
        price = fmt_money(st["last_ask"]) if st.get("last_ask") is not None else "rien en vente"
        mx = f" (max {fmt_money(w['max_price'])})" if w["max_price"] is not None else ""
        lines.append(f"• [{name}]({cfg.web_url(w['item'])}) : {price}{mx}")
    return lines


async def run_action(cfg: Config) -> int:
    store = Store(cfg.db_path)
    now = int(time.time())
    fresh = store.get_state("meta", "started") is None
    messages: list[dict] = []

    async with NGApi(cfg.base_url, cfg.server, cfg.api_key, cfg.request_delay, cfg.page_size) as api:
        sc = Scanner(cfg, api, store, watchlist_from_config_only=True)
        hits, deals = await sc.cycle()

    # Santé de l'API : prévenir après plusieurs échecs d'affilée, puis au retour
    fails = (store.get_state("meta", "failures") or {}).get("n", 0)
    if sc.last_error:
        fails += 1
        store.set_state("meta", "failures", {"n": fails})
        if fails == FAILURES_BEFORE_WARNING:
            messages.append(text_payload(cfg, f"⚠️ L'API NationsGlory ne répond plus depuis {fails} passages :\n"
                                              f"`{sc.last_error[:300]}`", color=ORANGE))
    else:
        if fails >= FAILURES_BEFORE_WARNING:
            messages.append(text_payload(cfg, "✅ L'API NationsGlory répond de nouveau."))
        store.set_state("meta", "failures", {"n": 0})

    # Premier passage (ou historique perdu) : message de confirmation avec l'état de la watchlist
    if fresh and not sc.last_error:
        store.set_state("meta", "started", {"ts": now})
        messages.append(text_payload(
            cfg, f"Je vérifie l'HDV de **{cfg.server.capitalize()}** toutes les ~15 min. "
                 f"Prix actuels de ta watchlist :",
            title="✅ Surveillance HDV démarrée", color=BLUE, ping=True, lines=_watch_lines(cfg, store) or ["(vide)"]))

    # Pings : seulement la watchlist (et les nouvelles affaires si deals.ping: true)
    messages += alert_payloads(cfg, hits, deals if cfg.ping_deals else [])
    # Récap sans ping des meilleures affaires du moment, à chaque passage
    if cfg.summary_enabled and cfg.deals_enabled and not sc.last_error:
        messages.append(summary_payload(cfg, sc.last_deals, cfg.summary_top))

    # Rappel avant la suspension automatique de GitHub (60 jours sans commit)
    last_commit = os.environ.get("HDVBOT_LAST_COMMIT_TS", "").strip()
    if last_commit.isdigit():
        days = (now - int(last_commit)) // 86400
        last_warn = (store.get_state("meta", "inactivity") or {}).get("ts", 0)
        if days >= INACTIVITY_WARN_DAYS and now - last_warn > 86400:
            store.set_state("meta", "inactivity", {"ts": now})
            messages.append(text_payload(
                cfg, f"⏰ Aucun commit sur le dépôt depuis {days} jours : GitHub suspend les tâches planifiées "
                     f"au bout de 60 jours. Fais un petit commit (par exemple modifie la watchlist dans config.yaml). "
                     f"Si c'est déjà suspendu : onglet **Actions** > le workflow > **Enable workflow**.",
                ping=True, color=ORANGE))

    if messages:
        await send_webhook(cfg.webhook_url, messages, username=f"HDV {cfg.server.capitalize()}")
    log.info("Webhook : %d message(s) envoyé(s) — %d alerte(s) watchlist, %d affaire(s)",
             len(messages), len(hits), len(deals))
    store.close()
    return 0
