"""Source NG Tools (https://ng-tools.fr/hdv) : pas besoin de clé API NationsGlory.

NG Tools relève l'HDV toutes les ~10 min et publie le cours de chaque objet sur sa page /hdv.
On lit cette page UNE fois par passage (toutes les ~15 min) : c'est la seule requête faite.
Leurs conditions interdisent seulement de surcharger le site ; robots.txt autorise /hdv.

Format d'une ligne du tableau (données intégrées à la page Next.js) :
  [id, nom, cours_7j_centimes, annonce_min_centimes, ?, variation_7j_bp, ?, vendus_7j, échangé_7j_$, nb_annonces, ...]
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .config import Config
from .logic import fmt_money

log = logging.getLogger("hdvbot.ngtools")

BASE = "https://ng-tools.fr"
USER_AGENT = "pawn-stars-ng/1.0 (bot Discord perso, 1 requete / 15 min)"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


class NGToolsError(Exception):
    pass


@dataclass
class Row:
    id: str            # "2535:8"
    name: str
    ref: float | None  # cours 7 j, $ / unité
    ask: float | None  # annonce la moins chère, $ / unité
    sold_7d: int
    listings: int

    @property
    def slug_id(self) -> str:
        return self.id.replace(":", "-")


def _cents(v) -> float | None:
    return None if v is None else v / 100


def parse_rows(html: str) -> list[Row]:
    """Extrait le tableau des objets de la page /hdv de NG Tools."""
    payload = "".join(json.loads(m.group(1)) for m in _PUSH.finditer(html))
    i = payload.find('"rows":[')
    if i < 0:
        raise NGToolsError("tableau des objets introuvable dans la page NG Tools (le site a peut-être changé)")
    raw, _ = json.JSONDecoder().raw_decode(payload, i + len('"rows":'))
    rows = []
    for r in raw:
        try:
            rows.append(Row(id=str(r[0]), name=str(r[1]), ref=_cents(r[2]), ask=_cents(r[3]),
                            sold_7d=int(r[7] or 0), listings=int(r[9] or 0)))
        except (IndexError, TypeError, ValueError):
            continue
    if not rows:
        raise NGToolsError("tableau des objets vide sur NG Tools")
    return rows


def item_url(cfg: Config, row_id: str) -> str:
    return cfg.web_url(row_id)


async def fetch_rows(server: str) -> list[Row]:
    url = f"{BASE}/hdv?serveur={server}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "fr"}) as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise NGToolsError(f"NG Tools a répondu {r.status} sur {url}")
            return parse_rows(await r.text())


# ---------- dernières ventes d'un objet (page /hdv/<serveur>/<id>) ----------

_A = re.compile(r'"children":"\s*à\s*"')
_CHILD = re.compile(r'"children":"([^"]*)"')
_DATE = re.compile(r'"dateTime":"([^"]+)"')


def _money(txt: str) -> float | None:
    t = re.sub(r"[\s\u00a0\u202f$]", "", txt).replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_sales(html: str) -> list[tuple[float, int, int]]:
    """Section « Dernières ventes » : [(prix unitaire $, quantité, timestamp)], la plus récente d'abord."""
    payload = "".join(json.loads(m.group(1)) for m in _PUSH.finditer(html))
    i = payload.find("Dernières ventes")
    if i < 0:
        raise NGToolsError("section « Dernières ventes » introuvable (le site a peut-être changé)")
    end = payload.find("Sur chaque serveur", i)
    sec = payload[i:end if end > 0 else len(payload)]
    from datetime import datetime
    out = []
    marks = list(_A.finditer(sec))
    for k, m in enumerate(marks):
        before = _CHILD.findall(sec[max(0, m.start() - 300):m.start()])
        nxt = marks[k + 1].start() if k + 1 < len(marks) else len(sec)
        after = sec[m.end():nxt]
        price = _CHILD.search(after)
        date = _DATE.search(after)
        if not before or not price:
            continue
        qty, unit = _money(before[-1]), _money(price.group(1))
        if qty is None or unit is None:
            continue
        ts = 0
        if date:
            try:
                ts = int(datetime.fromisoformat(date.group(1).replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        out.append((unit, int(qty), ts))
    return out


def recent_reference(sales: list, cfg: Config, now: int | None = None) -> tuple[float, int] | None:
    """Médiane des N dernières ventes, sans les transferts entre comptes (prix aberrants)."""
    s = cfg.ngtools
    now = now or int(time.time())
    recent = [p for p, _q, ts in sales if not ts or now - ts <= s["max_sale_age_days"] * 86400]
    recent = recent[: s["last_sales"] * 2]
    if not recent:
        return None
    med = statistics.median(recent[: s["last_sales"]])
    clean = [p for p in recent if med * 0.2 <= p <= med * 5][: s["last_sales"]]
    if len(clean) < s["min_sales"]:
        return None
    return statistics.median(clean), len(clean)


async def fetch_sales(session: aiohttp.ClientSession, server: str, item_id: str) -> list:
    url = f"{BASE}/hdv/{server}/{item_id.replace(':', '-')}"
    async with session.get(url) as r:
        if r.status != 200:
            raise NGToolsError(f"NG Tools a répondu {r.status} sur {url}")
        return parse_sales(await r.text())


def _candidates(rows: list[Row], cfg: Config, cache: dict, now: int) -> list[Row]:
    """Objets dont il faut (re)lire les dernières ventes, les plus prometteurs d'abord."""
    s = cfg.ngtools
    todo = []
    for r in rows:
        if r.ask is None or r.listings == 0:
            continue
        c = cache.get(r.id)
        ref_guess = max(r.ref or 0, (c or {}).get("ref") or 0)
        if ref_guess:
            if ref_guess < s["min_value"] * 0.5 or r.ask > ref_guess:
                continue
        elif r.ask < s["min_value"] * 0.05:      # aucun cours connu (objet rare) : on regarde quand même
            continue
        stale = c is None or c.get("sold") != r.sold_7d or now - c.get("t", 0) > s["sales_ttl_hours"] * 3600
        if stale:
            todo.append((0 if c is None else 1, -(ref_guess - r.ask), r))
    todo.sort(key=lambda x: (x[0], x[1]))
    return [r for *_k, r in todo]


async def refresh_sales(rows: list[Row], cfg: Config, state: dict, fetch=None) -> int:
    """Met à jour le cache des dernières ventes (quelques pages par passage, pour ménager NG Tools)."""
    s = cfg.ngtools
    cache = state.setdefault("sales", {})
    now = int(time.time())
    todo = _candidates(rows, cfg, cache, now)[: s["max_item_pages"]]
    if not todo:
        return 0
    done = 0

    async def run(fetcher):
        nonlocal done
        for k, r in enumerate(todo):
            if k:
                await asyncio.sleep(s["page_delay"])
            try:
                sales = await fetcher(r.id)
            except (NGToolsError, aiohttp.ClientError, TimeoutError, ValueError) as e:
                log.warning("Ventes de %s : %s", r.id, e)
                continue
            ref = recent_reference(sales, cfg, now)
            cache[r.id] = {"sold": r.sold_7d, "t": now, "ref": ref[0] if ref else None, "n": ref[1] if ref else 0}
            done += 1

    if fetch is not None:
        await run(fetch)
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                         headers={"User-Agent": USER_AGENT, "Accept-Language": "fr"}) as sess:
            await run(lambda item: fetch_sales(sess, cfg.server, item))
    log.info("Dernières ventes relues pour %d objet(s) (%d en attente)", done,
             max(0, len(_candidates(rows, cfg, cache, now))))
    return done


# ---------- logique ----------

@dataclass
class Deal:
    row: Row
    ref: float         # médiane des dernières ventes
    n: int             # nombre de ventes dans cette médiane
    discount: float
    gain: float        # gain par unité si on revend à ce prix


def find_deals(rows: list[Row], cfg: Config, state: dict) -> list[Deal]:
    s = cfg.ngtools
    cache = state.get("sales", {})
    out = []
    for r in rows:
        c = cache.get(r.id)
        if r.ask is None or not c or not c.get("ref") or c["ref"] < s["min_value"]:
            continue
        disc = 1 - r.ask / c["ref"]
        if disc >= s["min_discount"]:
            out.append(Deal(r, c["ref"], c["n"], disc, c["ref"] - r.ask))
    out.sort(key=lambda d: d.gain, reverse=True)
    return out


@dataclass
class WatchAlert:
    row: Row
    reason: str
    previous: float | None


def check_watch(rows: list[Row], cfg: Config, state: dict) -> list[WatchAlert]:
    """Ping quand un item suivi apparaît en vente, ou que son annonce la moins chère baisse."""
    by_id = {r.id: r for r in rows}
    seen = state.setdefault("watch", {})
    alerts = []
    for w in cfg.watchlist:
        r = by_id.get(w.item)
        if r is None:
            continue
        prev = seen.get(w.item)
        ask = r.ask
        seen[w.item] = ask
        if ask is None or (w.max_price is not None and ask > w.max_price):
            continue
        if prev is None:
            alerts.append(WatchAlert(r, "mis en vente", None))
        elif ask < prev * 0.999:
            alerts.append(WatchAlert(r, "moins cher", prev))
    return alerts


# ---------- rendu Discord ----------

def summary_embed(cfg: Config, deals: list[Deal], top: int) -> dict:
    s = cfg.ngtools
    if deals:
        lines = [f"**{i}. [{d.row.name}]({item_url(cfg, d.row.id)})** : {fmt_money(d.row.ask)} au lieu de "
                 f"~{fmt_money(d.ref)} (**−{d.discount:.0%}**) · gain ≈ {fmt_money(d.gain)}/u · "
                 f"{d.row.listings} annonce(s)" + (f" · ⚠️ seulement {d.n} ventes connues" if d.n < 5 else "")
                 for i, d in enumerate(deals[:top], 1)]
        if len(deals) > top:
            lines.append(f"… et {len(deals) - top} autre(s).")
        desc = "\n".join(lines)
    else:
        desc = "Aucune affaire en ce moment."
    desc += (f"\n\n*Prix de référence = médiane des {s['last_sales']} dernières ventes (transferts entre comptes "
             f"exclus, {s['min_sales']} ventes minimum). Objets d'au moins {fmt_money(s['min_value'])}, au moins "
             f"{s['min_discount']:.0%} en dessous. Classés par gain par unité. "
             f"Données [NG Tools]({BASE}/hdv?serveur={cfg.server}).*")
    return {"title": f"📊 Meilleures affaires HDV {cfg.server.capitalize()}", "description": desc[:4000],
            "color": 0x22C55E if deals else 0x6B7280,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def watch_embed(cfg: Config, a: WatchAlert) -> dict:
    r = a.row
    if a.reason == "mis en vente":
        head = f"**Mis en vente** à {fmt_money(r.ask)} / unité"
    else:
        head = f"**Moins cher** : {fmt_money(r.ask)} / unité (avant {fmt_money(a.previous)})"
    if r.ref:
        head += f" · cours 7 j ~{fmt_money(r.ref)} ({r.ask / r.ref - 1:+.0%})"
    return {"title": f"🔔 {r.name}"[:256], "url": item_url(cfg, r.id), "color": 0x3B82F6,
            "description": f"{head}\n{r.listings} annonce(s) en vente.",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------- passage complet ----------

def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def run_ngtools(cfg: Config, rows: list[Row] | None = None, fetch_item=None) -> int:
    from .action import send_webhook
    state_path = Path(cfg.db_path).with_name("ngtools_state.json")
    state = _load_state(state_path)
    fresh = "watch" not in state
    messages: list[dict] = []
    user = str(cfg.user_id) if cfg.user_id else None
    ping = {"content": f"<@{user}>" if user else None, "allowed_mentions": {"parse": [], "users": [user] if user else []}}

    try:
        rows = rows if rows is not None else await fetch_rows(cfg.server)
        error = None
    except (NGToolsError, aiohttp.ClientError, TimeoutError, ValueError) as e:
        rows, error = [], str(e)
        log.warning("NG Tools : %s", e)

    fails = state.get("failures", 0)
    if error:
        state["failures"] = fails + 1
        if fails + 1 == 3:
            messages.append({"embeds": [{"description": f"⚠️ NG Tools ne répond plus depuis 3 passages :\n`{error[:300]}`",
                                         "color": 0xF59E0B}], "allowed_mentions": {"parse": []}})
    else:
        if fails >= 3:
            messages.append({"embeds": [{"description": "✅ NG Tools répond de nouveau.", "color": 0x22C55E}],
                             "allowed_mentions": {"parse": []}})
        state["failures"] = 0
        by_id = {r.id: r for r in rows}
        alerts = check_watch(rows, cfg, state)
        if fresh:
            lines = []
            for w in cfg.watchlist:
                r = by_id.get(w.item)
                name = r.name if r else w.item.replace(":", "-")
                price = fmt_money(r.ask) if r and r.ask is not None else "rien en vente"
                lines.append(f"• [{name}]({cfg.web_url(w.item)}) : {price}")
            messages.append({**ping, "embeds": [{
                "title": "✅ Surveillance HDV démarrée", "color": 0x3B82F6,
                "description": "\n".join([f"Je vérifie l'HDV de **{cfg.server.capitalize()}** toutes les ~15 min "
                                          f"(données NG Tools). Je te @ quand un de ces items est mis en vente "
                                          f"ou baisse :"] + (lines or ["(watchlist vide)"]))}]})
            alerts = []   # premier passage : pas de ping pour ce qui est déjà en vente
        if alerts:
            embeds = [watch_embed(cfg, a) for a in alerts]
            for k in range(0, len(embeds), 10):
                messages.append({**(ping if k == 0 else {"allowed_mentions": {"parse": []}}), "embeds": embeds[k:k + 10]})
        if cfg.summary_enabled:
            await refresh_sales(rows, cfg, state, fetch=fetch_item)
            messages.append({"embeds": [summary_embed(cfg, find_deals(rows, cfg, state), cfg.summary_top)],
                             "allowed_mentions": {"parse": []}})

    # Rappel avant la suspension GitHub (60 jours sans commit)
    import os
    last_commit = os.environ.get("HDVBOT_LAST_COMMIT_TS", "").strip()
    now = int(time.time())
    if last_commit.isdigit():
        days = (now - int(last_commit)) // 86400
        if days >= 50 and now - state.get("inactivity_warn", 0) > 86400:
            state["inactivity_warn"] = now
            messages.append({**ping, "embeds": [{"color": 0xF59E0B, "description":
                f"⏰ Aucun commit sur le dépôt depuis {days} jours : GitHub suspend les tâches planifiées au bout "
                f"de 60 jours. Fais un petit commit (par exemple modifie la watchlist dans config.yaml)."}]})

    if messages:
        await send_webhook(cfg.webhook_url, messages, username="Pawn Stars NG")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    log.info("NG Tools : %d objets lus, %d message(s) envoyé(s)%s", len(rows), len(messages),
             f" — erreur : {error}" if error else "")
    return 0


async def dry_run(cfg: Config):
    """Diagnostic : lit NG Tools et affiche ce que le bot comprend, sans rien envoyer."""
    rows = await fetch_rows(cfg.server)
    by_id = {r.id: r for r in rows}
    print(f"NG Tools : {len(rows)} objets lus sur l'HDV {cfg.server}.")
    print("\n=== Watchlist ===")
    for w in cfg.watchlist:
        r = by_id.get(w.item)
        if r is None:
            print(f"  {w.item:>10}  INTROUVABLE sur NG Tools (vérifie l'ID)")
        else:
            print(f"  {w.item:>10}  {r.name[:30]:30} annonce min {fmt_money(r.ask):>12}  cours {fmt_money(r.ref):>12}"
                  f"  {r.listings} annonce(s)")
    state = _load_state(Path(cfg.db_path).with_name("ngtools_state.json"))
    await refresh_sales(rows, cfg, state)
    deals = find_deals(rows, cfg, state)
    print(f"\n=== Affaires ({len(deals)}) ===")
    for d in deals[:cfg.summary_top]:
        print(f"  {d.row.name[:30]:30} {fmt_money(d.row.ask):>12} au lieu de {fmt_money(d.ref):>12}"
              f" (médiane {d.n} ventes)  −{d.discount:.0%}  gain {fmt_money(d.gain)}/u")
