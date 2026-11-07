"""Chargement de config.yaml (+ variables d'environnement DISCORD_TOKEN / NG_API_KEY)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path

import yaml

from .fields import parse_item_ref
from .logic import DealSettings


@dataclass
class WatchEntry:
    item: str
    max_price: float | None = None
    mode: str = "cheaper"


@dataclass
class Config:
    discord_token: str = ""
    webhook_url: str = ""
    channel_id: int = 0
    user_id: int = 0
    guild_id: int | None = None
    api_key: str | None = None
    server: str = "ruby"
    base_url: str = "https://publicapi.nationsglory.fr/v1"
    request_delay: float = 0.5
    page_size: int = 100
    poll_minutes: float = 5
    watchlist: list[WatchEntry] = field(default_factory=list)
    deals_enabled: bool = True
    deals: DealSettings = field(default_factory=DealSettings)
    backfill_per_cycle: int = 25
    backfill_ttl_hours: float = 6
    max_alerts_per_cycle: int = 10
    summary_enabled: bool = True     # mode Actions : récap des meilleures affaires à chaque passage, sans ping
    summary_top: int = 10
    ping_deals: bool = False         # True = aussi un @ pour chaque nouvelle affaire
    fields: dict = field(default_factory=dict)
    db_path: str = "hdvbot.sqlite3"

    def web_url(self, item_id: str) -> str:
        return f"https://nationsglory.fr/servers/{self.server}/hdv/{item_id.replace(':', '-')}"


def load_config(path: str | os.PathLike = "config.yaml", require_discord: str | None = "bot") -> Config:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config introuvable : {p}. Copie config.example.yaml en config.yaml et remplis-la.")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    d = raw.get("discord") or {}
    ng = raw.get("nationsglory") or {}
    deals = raw.get("deals") or {}

    cfg = Config()
    cfg.discord_token = os.environ.get("DISCORD_TOKEN") or d.get("token") or ""
    cfg.webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or d.get("webhook_url") or "").strip()
    cfg.channel_id = int(d.get("channel_id") or 0)
    cfg.user_id = int((os.environ.get("DISCORD_USER_ID") or "").strip() or d.get("user_id") or 0)
    cfg.guild_id = int(d["guild_id"]) if d.get("guild_id") else None

    cfg.api_key = os.environ.get("NG_API_KEY") or ng.get("api_key") or None
    cfg.server = str(ng.get("server") or cfg.server).lower()
    cfg.base_url = ng.get("base_url") or cfg.base_url
    cfg.request_delay = float(ng.get("request_delay", cfg.request_delay))
    cfg.page_size = int(ng.get("page_size", cfg.page_size))

    cfg.poll_minutes = float(raw.get("poll_minutes", cfg.poll_minutes))
    cfg.max_alerts_per_cycle = int(raw.get("max_alerts_per_cycle", cfg.max_alerts_per_cycle))
    cfg.db_path = os.environ.get("HDVBOT_DB") or raw.get("db_path") or str(p.parent / cfg.db_path)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    for w in raw.get("watchlist") or []:
        if isinstance(w, (str, int)):
            w = {"item": str(w)}
        cfg.watchlist.append(WatchEntry(
            item=parse_item_ref(w["item"]),
            max_price=float(w["max_price"]) if w.get("max_price") is not None else None,
            mode=w.get("mode", "cheaper"),
        ))

    cfg.deals_enabled = bool(deals.get("enabled", True))
    known = {f.name for f in dc_fields(DealSettings)}
    cfg.deals = DealSettings(**{k: v for k, v in deals.items() if k in known})
    cfg.backfill_per_cycle = int(deals.get("backfill_per_cycle", cfg.backfill_per_cycle))
    cfg.backfill_ttl_hours = float(deals.get("backfill_ttl_hours", cfg.backfill_ttl_hours))
    summ = raw.get("summary") or {}
    cfg.summary_enabled = bool(summ.get("enabled", cfg.summary_enabled))
    cfg.summary_top = int(summ.get("top", cfg.summary_top))
    cfg.ping_deals = bool(deals.get("ping", cfg.ping_deals))
    cfg.fields = dict(raw.get("fields") or {})

    if require_discord == "bot":
        missing = [n for n, v in (("discord.token / DISCORD_TOKEN", cfg.discord_token),
                                  ("discord.channel_id", cfg.channel_id),
                                  ("discord.user_id / DISCORD_USER_ID", cfg.user_id)) if not v]
        if missing:
            raise SystemExit("Config incomplète : " + ", ".join(missing))
    elif require_discord == "webhook":
        if not cfg.webhook_url.startswith(("https://", "http://")):
            raise SystemExit("Webhook Discord manquant : ajoute le secret DISCORD_WEBHOOK_URL "
                             "(Paramètres du dépôt > Secrets and variables > Actions).")
    return cfg
