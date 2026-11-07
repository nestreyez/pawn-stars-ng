"""Moteur de surveillance, indépendant de Discord (utilisé par le bot et par `python -m hdvbot once`)."""
from __future__ import annotations

import logging
import time
from dataclasses import replace

from .api import ApiError, NGApi, NotFound
from .config import Config
from .fields import ItemSnap, Trade, normalize_item, normalize_trade
from .logic import (Deal, Reference, WatchHit, deal_gate, evaluate_deal, pick_reference,
                    reference_from_trades, watch_gate)
from .store import Store

log = logging.getLogger("hdvbot.scanner")


class Scanner:
    def __init__(self, cfg: Config, api: NGApi, store: Store, watchlist_from_config_only: bool = False):
        self.cfg = cfg
        self.api = api
        self.store = store
        self.ov = cfg.fields
        self.global_trades_ok = True
        self.last_snaps: dict[str, ItemSnap] = {}
        self.last_deals: list[Deal] = []
        self.last_cycle_ts: int | None = None
        self.last_error: str | None = None
        self._last_prune = 0
        for w in cfg.watchlist:
            store.watch_add(w.item, w.max_price, w.mode)
        if watchlist_from_config_only:   # mode GitHub Actions : config.yaml fait foi
            wanted = {w.item for w in cfg.watchlist}
            for row in store.watch_list():
                if row["item"] not in wanted:
                    store.watch_remove(row["item"])

    # --- helpers ------------------------------------------------------------------------
    def _since(self) -> int:
        return int(time.time()) - self.cfg.deals.max_trade_age_days * 86400

    def local_trades(self, item: str) -> list[Trade]:
        return self.store.recent_trades(item, self._since(), limit=max(60, self.cfg.deals.last_trades * 6))

    def reference(self, item: str, snap: ItemSnap | None) -> Reference | None:
        return pick_reference(self.local_trades(item), snap, self.cfg.deals)

    def name_of(self, item: str, snap: ItemSnap | None = None) -> str | None:
        return (snap.name if snap and snap.name else None) or self.store.get_name(item)

    # --- étapes du cycle ---------------------------------------------------------------
    async def sync_trades(self) -> int:
        """Récupère le flux global des dernières ventes et les stocke (historique local)."""
        if not self.global_trades_ok:
            return 0
        first_run = self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0

        def stop(page: list) -> bool:
            for r in page:
                t = normalize_trade(r, None, self.ov) if isinstance(r, dict) else None
                if t and self.store.has_trade(t.uid):
                    return True
            return False

        try:
            raw = await self.api.trades(limit=self.cfg.page_size, max_pages=20 if first_run else 5, stop=stop)
        except NotFound:
            self.global_trades_ok = False
            log.info("Pas de flux global des ventes : historique récupéré item par item.")
            return 0
        except ApiError as e:
            if str(e).startswith("400"):
                self.global_trades_ok = False
                log.info("Le flux global des ventes exige un item : historique item par item.")
            else:
                log.warning("Ventes : %s", e)
            return 0
        trades = [t for t in (normalize_trade(r, None, self.ov) for r in raw) if t]
        added = self.store.add_trades(trades)
        log.info("Ventes : %d reçues, %d nouvelles", len(raw), added)
        return added

    async def fetch_items(self) -> dict[str, ItemSnap]:
        try:
            raw = await self.api.items()
        except ApiError as e:
            log.warning("Liste des items : %s", e)
            self.last_error = str(e)
            return {}
        self.last_error = None
        snaps: dict[str, ItemSnap] = {}
        for r in raw:
            s = normalize_item(r, self.ov)
            if s:
                snaps[s.id] = s
        self.store.set_names((s.id, s.name) for s in snaps.values() if s.name)
        log.info("Items HDV : %d (dont %d avec une annonce)", len(snaps),
                 sum(1 for s in snaps.values() if s.ask is not None))
        self.last_snaps = snaps
        return snaps

    async def item_snapshot(self, item: str, listed: ItemSnap | None = None) -> ItemSnap:
        """Détail d'un item (annonces + dernières ventes), complété par la liste si besoin.
        Lève ApiError en cas d'erreur réseau pour ne pas confondre avec "plus en vente"."""
        try:
            rec = await self.api.item(item)
            snap = normalize_item(rec, self.ov, item_hint=item) or ItemSnap(id=item)
        except NotFound:
            snap = ItemSnap(id=item)
        if snap.trades:
            self.store.add_trades(snap.trades)
        if listed:
            if snap.ask is None and not snap.offers and listed.ask is not None:
                snap = replace(snap, ask=listed.ask, ask_qty=listed.ask_qty, count=listed.count)
            snap = replace(snap, name=snap.name or listed.name, api_ref=snap.api_ref or listed.api_ref)
        if not snap.name:
            snap = replace(snap, name=self.store.get_name(item))
        else:
            self.store.set_names([(item, snap.name)])
        return snap

    async def backfill(self, item: str) -> int:
        try:
            raw = await self.api.trades(item=item, limit=min(100, max(30, self.cfg.deals.last_trades * 5)))
        except ApiError as e:
            log.debug("Historique %s : %s", item, e)
            return 0
        trades = [t for t in (normalize_trade(r, item, self.ov) for r in raw) if t]
        self.store.add_trades(trades)
        self.store.set_state("backfill", item, {"n": len(trades)})
        return len(trades)

    async def check_watchlist(self, snaps: dict[str, ItemSnap]) -> list[WatchHit]:
        hits: list[WatchHit] = []
        for w in self.store.watch_list():
            item = w["item"]
            try:
                snap = await self.item_snapshot(item, snaps.get(item))
            except ApiError as e:
                log.warning("Watch %s : %s (ignoré ce cycle)", item, e)
                continue
            state = self.store.get_state("watch", item)
            reason, fresh, new_state = watch_gate(state, snap, w["max_price"], w["mode"])
            self.store.set_state("watch", item, new_state)
            if reason:
                hits.append(WatchHit(item=item, name=self.name_of(item, snap), reason=reason,
                                     ask=snap.ask, ask_qty=snap.ask_qty, count=snap.count,
                                     max_price=w["max_price"], ref=self.reference(item, snap),
                                     new_offers=fresh))
        return hits

    async def scan_deals(self, snaps: dict[str, ItemSnap]) -> list[Deal]:
        s = self.cfg.deals
        now = int(time.time())
        ttl = self.cfg.backfill_ttl_hours * 3600
        since = self._since()
        current: list[Deal] = []
        need: list[tuple[tuple, ItemSnap]] = []

        def api_deal(snap: ItemSnap) -> Deal | None:
            if s.use_api_ref_fallback and snap.api_ref:
                return evaluate_deal(snap, Reference(snap.api_ref, 0, "api"), s)
            return None

        for snap in snaps.values():
            if snap.ask is None:
                continue
            local = reference_from_trades(self.local_trades(snap.id), s)
            if local and local.n >= s.min_trades:
                d = evaluate_deal(snap, local, s)
                if d:
                    current.append(d)
                continue
            last_bf = self.store.state_ts("backfill", snap.id)
            if last_bf and now - last_bf < ttl:
                d = api_deal(snap)          # item peu échangé : on se rabat sur l'API si dispo
                if d:
                    current.append(d)
                continue
            local_max = self.store.max_trade_price(snap.id, since) or 0
            hint = max(snap.api_ref or 0, local_max, snap.ask)
            if (snap.api_ref or local_max) and max(snap.api_ref or 0, local_max) < s.min_value * 0.5:
                continue                     # clairement pas un item à 2k
            need.append(((1 if api_deal(snap) else 0, hint), snap))

        need.sort(key=lambda x: x[0], reverse=True)
        for _, snap in need[: self.cfg.backfill_per_cycle]:
            await self.backfill(snap.id)
            d = evaluate_deal(snap, self.reference(snap.id, snap), s)
            if d:
                current.append(d)
        if need:
            log.info("Historique : %d items complétés ce cycle, %d en attente",
                     min(len(need), self.cfg.backfill_per_cycle), max(0, len(need) - self.cfg.backfill_per_cycle))

        current.sort(key=lambda d: d.profit, reverse=True)
        self.last_deals = current

        alerts: list[Deal] = []
        live = {d.item for d in current}
        for d in current:
            ok, new_state = deal_gate(self.store.get_state("deal", d.item), d.ask, s.realert_drop)
            self.store.set_state("deal", d.item, new_state)
            if ok:
                alerts.append(d)
        if snaps:
            for item in self.store.state_items("deal"):
                if item not in live:
                    self.store.clear_state("deal", item)   # affaire partie : on ré-arme

        # Détail des items qui vont pinger : quantités, vendeurs, et annonce toujours là ?
        enriched: list[Deal] = []
        for d in alerts:
            if len(enriched) >= self.cfg.max_alerts_per_cycle:
                enriched.append(d)
                continue
            try:
                snap = await self.item_snapshot(d.item, snaps.get(d.item))
            except ApiError:
                enriched.append(d)
                continue
            d2 = evaluate_deal(snap, d.ref, s)
            if d2:
                self.store.set_state("deal", d.item, {"alert_price": d2.ask})
                enriched.append(d2)
                self.last_deals = [d2 if x.item == d2.item else x for x in self.last_deals]
            else:
                self.store.clear_state("deal", d.item)
                self.last_deals = [x for x in self.last_deals if x.item != d.item]
        enriched.sort(key=lambda d: d.profit, reverse=True)
        self.last_deals.sort(key=lambda d: d.profit, reverse=True)
        return enriched

    async def cycle(self) -> tuple[list[WatchHit], list[Deal]]:
        t0 = time.monotonic()
        await self.sync_trades()
        snaps = await self.fetch_items()
        hits = await self.check_watchlist(snaps)
        deals = await self.scan_deals(snaps) if self.cfg.deals_enabled and snaps else []
        now = int(time.time())
        if now - self._last_prune > 86400:
            self.store.prune_trades(now - 120 * 86400)
            self._last_prune = now
        self.last_cycle_ts = now
        log.info("Cycle terminé en %.1fs : %d alerte(s) watchlist, %d bonne(s) affaire(s)",
                 time.monotonic() - t0, len(hits), len(deals))
        return hits, deals

    async def lookup(self, item: str) -> tuple[ItemSnap, Reference | None, list[Trade]]:
        snap = await self.item_snapshot(item, self.last_snaps.get(item))
        if len(self.local_trades(item)) < self.cfg.deals.min_trades:
            await self.backfill(item)
        trades = self.local_trades(item)
        return snap, pick_reference(trades, snap, self.cfg.deals), trades
