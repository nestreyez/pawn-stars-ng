"""Bot Discord : boucle de surveillance, pings, commandes /watch, /prix, /deals, /scan."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

from .api import ApiError
from .config import Config
from .fields import ItemSnap, parse_item_ref
from .logic import Deal, Reference, WatchHit, fmt_money, watch_gate
from .render import GREEN, GREY, alert_payloads, ref_text as _ref_text, vs_ref as _vs_ref
from .scanner import Scanner

log = logging.getLogger("hdvbot.bot")

class HDVBot(discord.Client):
    def __init__(self, cfg: Config, scanner: Scanner):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.scanner = scanner
        self.tree = app_commands.CommandTree(self)
        self.wake = asyncio.Event()
        self._channel: discord.abc.Messageable | None = None
        register_commands(self)

    async def setup_hook(self):
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.loop.create_task(self.poll_loop())

    async def on_ready(self):
        log.info("Connecté en tant que %s — serveur NationsGlory : %s", self.user, self.cfg.server)

    async def channel(self) -> discord.abc.Messageable:
        if self._channel is None:
            self._channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(self.cfg.channel_id)
        return self._channel

    # --- envoi ----------------------------------------------------------------------------------
    async def send_alerts(self, hits: list[WatchHit], deals: list[Deal]):
        ch = await self.channel()
        for p in alert_payloads(self.cfg, hits, deals):
            content = p["content"]
            if content and "sous le seuil." in content:
                content = content.replace("sous le seuil.", "sous le seuil : tape `/deals`.")
            await ch.send(content=content, embeds=[discord.Embed.from_dict(e) for e in p["embeds"]],
                          allowed_mentions=discord.AllowedMentions(
                              users=[discord.Object(id=self.cfg.user_id)], everyone=False, roles=False))

    async def poll_loop(self):
        await self.wait_until_ready()
        failures, warned = 0, False
        while not self.is_closed():
            try:
                hits, deals = await self.scanner.cycle()
                await self.send_alerts(hits, deals)
                if self.scanner.last_error:
                    raise ApiError(self.scanner.last_error)
                if warned:
                    await (await self.channel()).send("✅ L'API NationsGlory répond de nouveau.")
                failures, warned = 0, False
            except Exception as e:  # on ne laisse jamais mourir la boucle
                failures += 1
                log.exception("Cycle en erreur (%d d'affilée)", failures)
                if failures >= 3 and not warned:
                    warned = True
                    try:
                        await (await self.channel()).send(
                            f"⚠️ Surveillance HDV en échec depuis {failures} cycles : `{str(e)[:300]}`")
                    except discord.DiscordException:
                        pass
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.cfg.poll_minutes * 60)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()


# --- Commandes slash --------------------------------------------------------------------------------
def register_commands(bot: HDVBot):
    cfg, scanner, store = bot.cfg, bot.scanner, bot.scanner.store
    watch = app_commands.Group(name="watch", description="Surveiller des items de l'HDV")

    def is_owner(inter: discord.Interaction) -> bool:
        return inter.user.id == cfg.user_id

    async def status_embed(item: str, snap: ItemSnap, ref: Reference | None, title_prefix: str) -> discord.Embed:
        e = discord.Embed(title=f"{title_prefix}{snap.label}", url=cfg.web_url(item), color=GREY)
        if snap.ask is not None:
            qty = f" (lot de {snap.ask_qty})" if snap.ask_qty and snap.ask_qty > 1 else ""
            e.add_field(name="Moins cher en vente", value=f"{fmt_money(snap.ask)} / unité{qty}{_vs_ref(snap.ask, ref)}",
                        inline=False)
        else:
            e.add_field(name="Moins cher en vente", value="rien en vente actuellement", inline=False)
        if snap.count is not None:
            e.add_field(name="En vente", value=str(snap.count))
        e.add_field(name="Référence", value=_ref_text(ref), inline=False)
        return e

    @watch.command(name="add", description="Te ping quand cet item est mis en vente")
    @app_commands.describe(item="ID (ex. 2535-8) ou lien de la page HDV",
                           max_price="Prix unitaire max (facultatif)",
                           mode="Quand te prévenir")
    @app_commands.choices(mode=[
        app_commands.Choice(name="nouvelle annonce moins chère (défaut)", value="cheaper"),
        app_commands.Choice(name="chaque nouvelle annonce", value="all"),
    ])
    async def watch_add(inter: discord.Interaction, item: str, max_price: float | None = None,
                        mode: app_commands.Choice[str] | None = None):
        if not is_owner(inter):
            return await inter.response.send_message("Seul le propriétaire du bot peut modifier la watchlist.",
                                                     ephemeral=True)
        try:
            item_id = parse_item_ref(item)
        except ValueError as e:
            return await inter.response.send_message(str(e), ephemeral=True)
        await inter.response.defer(thinking=True)
        store.watch_add(item_id, max_price, mode.value if mode else "cheaper")
        try:
            snap, ref, _ = await scanner.lookup(item_id)
        except ApiError as e:
            return await inter.followup.send(f"Ajouté `{item_id}`, mais l'API ne répond pas : {e}")
        store.set_state("watch", item_id, watch_gate(None, snap, max_price)[2])
        e = await status_embed(item_id, snap, ref, "👀 Surveillé : ")
        if max_price is not None:
            e.add_field(name="Ton prix max", value=fmt_money(max_price))
            if snap.ask is not None and snap.ask <= max_price:
                e.description = "⚡ Il y a **déjà** une annonce sous ton prix max !"
        await inter.followup.send(embed=e)

    @watch.command(name="remove", description="Arrêter de surveiller un item")
    @app_commands.describe(item="ID (ex. 2535-8) ou lien de la page HDV")
    async def watch_remove(inter: discord.Interaction, item: str):
        if not is_owner(inter):
            return await inter.response.send_message("Seul le propriétaire du bot peut modifier la watchlist.",
                                                     ephemeral=True)
        try:
            item_id = parse_item_ref(item)
        except ValueError as e:
            return await inter.response.send_message(str(e), ephemeral=True)
        ok = store.watch_remove(item_id)
        note = " (il est aussi dans config.yaml : retire-le là-bas sinon il reviendra au redémarrage)" \
            if any(w.item == item_id for w in cfg.watchlist) else ""
        await inter.response.send_message(f"{'Retiré' if ok else 'Pas dans la watchlist'} : `{item_id}`{note}")

    @watch.command(name="list", description="Voir les items surveillés")
    async def watch_list(inter: discord.Interaction):
        rows = store.watch_list()
        if not rows:
            return await inter.response.send_message("Watchlist vide. Ajoute un item avec `/watch add`.")
        lines = []
        for r in rows:
            st = store.get_state("watch", r["item"]) or {}
            name = store.get_name(r["item"]) or r["item"]
            mx = f" · max {fmt_money(r['max_price'])}" if r["max_price"] is not None else ""
            now = f" · actuellement {fmt_money(st['last_ask'])}" if st.get("last_ask") is not None else ""
            lines.append(f"• [{name}]({cfg.web_url(r['item'])}) `{r['item'].replace(':', '-')}`{mx}{now}")
        e = discord.Embed(title="👀 Watchlist", description="\n".join(lines)[:4000], color=GREY)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="prix", description="Prix actuel et référence d'un item")
    @app_commands.describe(item="ID (ex. 2535-8) ou lien de la page HDV")
    async def prix(inter: discord.Interaction, item: str):
        try:
            item_id = parse_item_ref(item)
        except ValueError as e:
            return await inter.response.send_message(str(e), ephemeral=True)
        await inter.response.defer(thinking=True)
        try:
            snap, ref, trades = await scanner.lookup(item_id)
        except ApiError as e:
            return await inter.followup.send(f"L'API ne répond pas : {e}")
        e = await status_embed(item_id, snap, ref, "")
        if trades:
            lines = [f"• {t.qty} × {fmt_money(t.price)} — <t:{t.ts}:R>" for t in trades[:6]]
            e.add_field(name="Dernières ventes", value="\n".join(lines), inline=False)
        await inter.followup.send(embed=e)

    @app_commands.command(name="deals", description="Bonnes affaires détectées au dernier passage")
    async def deals(inter: discord.Interaction):
        ds = scanner.last_deals
        if not ds:
            msg = "Aucune bonne affaire au dernier passage."
            if scanner.last_cycle_ts is None:
                msg = "Premier passage en cours, réessaie dans quelques minutes."
            return await inter.response.send_message(msg)
        lines = [f"• [{d.label}]({cfg.web_url(d.item)}) : {fmt_money(d.ask)} au lieu de ~{fmt_money(d.ref.price)} "
                 f"(**−{d.discount:.0%}**, gain ~{fmt_money(d.profit)})" for d in ds[:15]]
        e = discord.Embed(title=f"💰 Bonnes affaires — {cfg.server.capitalize()}",
                          description="\n".join(lines)[:4000], color=GREEN)
        if scanner.last_cycle_ts:
            e.set_footer(text="Dernier passage")
            e.timestamp = datetime.fromtimestamp(scanner.last_cycle_ts, timezone.utc)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="scan", description="Forcer un passage de surveillance maintenant")
    async def scan(inter: discord.Interaction):
        if not is_owner(inter):
            return await inter.response.send_message("Réservé au propriétaire du bot.", ephemeral=True)
        bot.wake.set()
        await inter.response.send_message("🔎 Passage lancé, les alertes arriveront ici.", ephemeral=True)

    bot.tree.add_command(watch)
    bot.tree.add_command(prix)
    bot.tree.add_command(deals)
    bot.tree.add_command(scan)
