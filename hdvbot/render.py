"""Rendu des alertes en JSON Discord (embeds), commun au bot et au webhook. Sans dépendance à discord.py."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .logic import Deal, Reference, WatchHit, fmt_money

BLUE = 0x3B82F6
GREEN = 0x22C55E
GREY = 0x6B7280
ORANGE = 0xF59E0B


def ref_text(ref: Reference | None) -> str:
    if ref is None:
        return "pas assez de ventes connues"
    if ref.source == "api":
        return f"{fmt_money(ref.price)} (cours fourni par l'API)"
    return f"{fmt_money(ref.price)} (médiane des {ref.n} dernières ventes)"


def vs_ref(price: float | None, ref: Reference | None) -> str:
    if price is None or ref is None or ref.price <= 0:
        return ""
    return f" ({price / ref.price - 1:+.0%} vs réf.)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _footer(cfg: Config, item: str) -> dict:
    return {"text": f"{cfg.server.capitalize()} · {item.replace(':', '-')} · données API (~15-30 min)"}


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value[:1024], "inline": inline}


def watch_embed(cfg: Config, h: WatchHit) -> dict:
    qty = f" (lot de {h.ask_qty})" if h.ask_qty and h.ask_qty > 1 else ""
    fields = []
    if h.count is not None:
        fields.append(_field("En vente", str(h.count)))
    if h.max_price is not None:
        fields.append(_field("Ton prix max", fmt_money(h.max_price)))
    fields.append(_field("Référence", ref_text(h.ref), inline=False))
    if h.new_offers:
        lines = [f"• {o.qty} × {fmt_money(o.price)}" + (f" — {o.seller}" if o.seller else "")
                 for o in h.new_offers[:5]]
        fields.append(_field("Annonces", "\n".join(lines), inline=False))
    return {
        "title": f"🔔 {h.label}"[:256], "url": cfg.web_url(h.item), "color": BLUE,
        "description": f"**{h.reason.capitalize()}** : {fmt_money(h.ask)} / unité{qty}{vs_ref(h.ask, h.ref)}",
        "fields": fields, "footer": _footer(cfg, h.item), "timestamp": _now_iso(),
    }


def deal_embed(cfg: Config, d: Deal) -> dict:
    fields = [_field("Quantité sous le seuil", str(d.cheap_qty)), _field("Gain potentiel", fmt_money(d.profit))]
    if d.count is not None:
        fields.append(_field("En vente", str(d.count)))
    fields.append(_field("Référence", ref_text(d.ref), inline=False))
    return {
        "title": f"💰 {d.label}"[:256], "url": cfg.web_url(d.item), "color": GREEN,
        "description": (f"**{fmt_money(d.ask)}** / unité au lieu de ~{fmt_money(d.ref.price)} "
                         f"→ **−{d.discount:.0%}**"),
        "fields": fields, "footer": _footer(cfg, d.item), "timestamp": _now_iso(),
    }


def mention(cfg: Config) -> str:
    return f"<@{cfg.user_id}>" if cfg.user_id else ""


def allowed_mentions(cfg: Config) -> dict:
    return {"parse": [], "users": [str(cfg.user_id)] if cfg.user_id else []}


def alert_payloads(cfg: Config, hits: list[WatchHit], deals: list[Deal]) -> list[dict]:
    """Messages prêts à envoyer (10 embeds max par message, ping sur le premier)."""
    if not hits and not deals:
        return []
    cap = cfg.max_alerts_per_cycle
    embeds = [watch_embed(cfg, h) for h in hits] + [deal_embed(cfg, d) for d in deals[:cap]]
    extra = max(0, len(deals) - cap)
    out = []
    for i in range(0, len(embeds), 10):
        content = mention(cfg) if i == 0 else ""
        if i + 10 >= len(embeds) and extra:
            content += f"\n… et {extra} autre(s) affaire(s) sous le seuil."
        out.append({"content": content.strip() or None, "embeds": embeds[i:i + 10],
                    "allowed_mentions": allowed_mentions(cfg)})
    return out


def text_payload(cfg: Config, text: str, ping: bool = False, color: int = GREY, title: str | None = None,
                 lines: list[str] | None = None) -> dict:
    embed = {"description": "\n".join([text] + (lines or []))[:4000], "color": color, "timestamp": _now_iso()}
    if title:
        embed["title"] = title[:256]
    return {"content": mention(cfg) if ping and cfg.user_id else None, "embeds": [embed],
            "allowed_mentions": allowed_mentions(cfg)}


def summary_payload(cfg: Config, deals: list[Deal], top: int) -> dict:
    """Récap des meilleures affaires du moment, sans ping."""
    s = cfg.deals
    if deals:
        lines = []
        for i, d in enumerate(deals[:top], 1):
            lines.append(f"**{i}. [{d.label}]({cfg.web_url(d.item)})** : {fmt_money(d.ask)} au lieu de "
                         f"~{fmt_money(d.ref.price)} (**−{d.discount:.0%}**) · {d.cheap_qty} dispo · "
                         f"gain ≈ {fmt_money(d.profit)}")
        if len(deals) > top:
            lines.append(f"… et {len(deals) - top} autre(s).")
        desc = "\n".join(lines)
    else:
        desc = "Aucune affaire en ce moment."
    desc += (f"\n\n*Critères : item ≥ {fmt_money(s.min_value)}, au moins {s.min_discount:.0%} sous la médiane "
             f"des {s.last_trades} dernières ventes. Classé par gain potentiel.*")
    embed = {"title": f"📊 Meilleures affaires HDV {cfg.server.capitalize()}",
             "description": desc[:4000], "color": GREEN if deals else GREY, "timestamp": _now_iso()}
    return {"content": None, "embeds": [embed], "allowed_mentions": {"parse": []}}
