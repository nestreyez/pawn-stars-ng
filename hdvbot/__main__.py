"""Point d'entrée :

    python -m hdvbot            # lance le bot Discord
    python -m hdvbot probe      # diagnostic de l'API (sans Discord)
    python -m hdvbot once       # un passage de surveillance, résultats dans le terminal (sans Discord)
    python -m hdvbot action     # un passage + alertes par webhook Discord (mode GitHub Actions)
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .api import NGApi
from .config import load_config
from .fields import parse_item_ref
from .logic import fmt_money
from .scanner import Scanner
from .store import Store


def setup_logging(verbose: bool):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("discord").setLevel(logging.WARNING)


async def run_bot(cfg):
    from .bot import HDVBot
    store = Store(cfg.db_path)
    async with NGApi(cfg.base_url, cfg.server, cfg.api_key, cfg.request_delay, cfg.page_size) as api:
        bot = HDVBot(cfg, Scanner(cfg, api, store))
        async with bot:
            await bot.start(cfg.discord_token)


async def run_once(cfg):
    store = Store(cfg.db_path)
    async with NGApi(cfg.base_url, cfg.server, cfg.api_key, cfg.request_delay, cfg.page_size) as api:
        sc = Scanner(cfg, api, store)
        hits, new_deals = await sc.cycle()
        print("\n=== Watchlist ===")
        for w in store.watch_list():
            st = store.get_state("watch", w["item"]) or {}
            print(f"  {w['item']:>10}  {store.get_name(w['item']) or '?':30}  moins cher : {fmt_money(st.get('last_ask'))}"
                  f"  (en vente : {st.get('count') or 0})")
        for h in hits:
            print(f"  🔔 {h.label} : {h.reason} à {fmt_money(h.ask)}")
        print(f"\n=== Bonnes affaires ({len(sc.last_deals)} au total, {len(new_deals)} nouvelles) ===")
        for d in sc.last_deals[:20]:
            print(f"  💰 {d.label[:30]:30} {fmt_money(d.ask):>12} au lieu de ~{fmt_money(d.ref.price):>12}"
                  f"  −{d.discount:.0%}  gain ~{fmt_money(d.profit)}  [{d.ref.source}, n={d.ref.n}]")
        n = store.db.execute("SELECT COUNT(*), COUNT(DISTINCT item) FROM trades").fetchone()
        print(f"\nHistorique local : {n[0]} ventes sur {n[1]} items (il se remplit au fil des passages).")


def main():
    ap = argparse.ArgumentParser(prog="hdvbot", description="Bot Discord HDV NationsGlory")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "probe", "once", "action"])
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-i", "--item", help="item à tester avec probe (ex. 2535-8)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    if args.command == "probe":
        from .probe import run_probe
        cfg = load_config(args.config, require_discord=None)
        asyncio.run(run_probe(cfg, parse_item_ref(args.item) if args.item else None))
    elif args.command == "once":
        asyncio.run(run_once(load_config(args.config, require_discord=None)))
    elif args.command == "action":
        from .action import run_action
        raise SystemExit(asyncio.run(run_action(load_config(args.config, require_discord="webhook"))))
    else:
        try:
            asyncio.run(run_bot(load_config(args.config, require_discord="bot")))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
