"""Logique pure (testable sans réseau) : prix de référence, bonnes affaires, anti-spam."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .fields import ItemSnap, Offer, Trade


@dataclass
class DealSettings:
    min_value: float = 2000.0       # prix de référence unitaire minimum
    min_discount: float = 0.30      # décote minimale (0.30 = 30 % sous la référence)
    min_profit: float = 0.0         # gain potentiel minimum (ref - prix) × quantité
    last_trades: int = 10           # référence = médiane des X dernières ventes
    min_trades: int = 5             # nb minimum de ventes pour faire confiance à la référence
    max_trade_age_days: int = 30
    outlier_low: float = 0.2        # ventes < 20 % de la médiane brute = transferts, ignorées
    outlier_high: float = 5.0
    realert_drop: float = 0.05      # re-ping si l'annonce baisse encore de 5 %
    use_api_ref_fallback: bool = True


@dataclass
class Reference:
    price: float
    n: int
    source: str          # "ventes" ou "api"


@dataclass
class Deal:
    item: str
    name: str | None
    ask: float
    ask_qty: int
    ref: Reference
    discount: float
    profit: float               # gain si on rachète toutes les annonces sous le seuil
    cheap_qty: int              # quantité totale sous le seuil
    count: int | None

    @property
    def label(self) -> str:
        return self.name or self.item


@dataclass
class WatchHit:
    item: str
    name: str | None
    reason: str
    ask: float
    ask_qty: int | None
    count: int | None
    max_price: float | None
    ref: Reference | None = None
    new_offers: list[Offer] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or self.item


# --- Prix de référence ----------------------------------------------------------
def collapse(trades: list[Trade]) -> list[Trade]:
    """Fusionne les ventes identiques dans la même heure (transferts répétés ×40 d'affilée)."""
    seen: set[tuple] = set()
    out = []
    for t in sorted(trades, key=lambda t: t.ts, reverse=True):
        key = (t.ts // 3600, round(t.price, 4), t.qty)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def reference_from_trades(trades: list[Trade], s: DealSettings) -> Reference | None:
    window = collapse(trades)[: max(s.last_trades * 3, s.last_trades)]
    if not window:
        return None
    raw = median(t.price for t in window)
    if raw <= 0:
        return None
    kept = [t for t in window if s.outlier_low * raw <= t.price <= s.outlier_high * raw][: s.last_trades]
    if not kept:
        return None
    return Reference(price=median(t.price for t in kept), n=len(kept), source="ventes")


def pick_reference(trades: list[Trade], snap: ItemSnap | None, s: DealSettings) -> Reference | None:
    ref = reference_from_trades(trades, s)
    if ref and ref.n >= s.min_trades:
        return ref
    if s.use_api_ref_fallback and snap and snap.api_ref:
        return Reference(price=snap.api_ref, n=0, source="api")
    return None


# --- Bonnes affaires -------------------------------------------------------------
def evaluate_deal(snap: ItemSnap, ref: Reference | None, s: DealSettings) -> Deal | None:
    if ref is None or snap.ask is None or snap.ask <= 0:
        return None
    if ref.price < s.min_value:
        return None
    discount = 1 - snap.ask / ref.price
    if discount < s.min_discount:
        return None
    threshold = ref.price * (1 - s.min_discount)
    if snap.offers:
        cheap = [o for o in snap.offers if o.price <= threshold]
        profit = sum((ref.price - o.price) * o.qty for o in cheap)
        cheap_qty = sum(o.qty for o in cheap)
    else:
        q = snap.ask_qty or 1
        profit = (ref.price - snap.ask) * q
        cheap_qty = q
    if profit < s.min_profit:
        return None
    return Deal(item=snap.id, name=snap.name, ask=snap.ask, ask_qty=snap.ask_qty or 1, ref=ref,
                discount=discount, profit=profit, cheap_qty=cheap_qty, count=snap.count)


def deal_gate(state: dict | None, ask: float, realert_drop: float) -> tuple[bool, dict]:
    """Décide si on ping pour une affaire, en évitant de répéter la même annonce."""
    last = (state or {}).get("alert_price")
    if last is None:
        return True, {"alert_price": ask}
    if ask < last * (1 - realert_drop):
        return True, {"alert_price": ask}
    # Même annonce, ou la moins chère est partie et la suivante est un peu plus haute :
    # on suit le prix sans re-pinger.
    return False, {"alert_price": ask}


# --- Watchlist ----------------------------------------------------------------------
def watch_gate(state: dict | None, snap: ItemSnap, max_price: float | None,
               mode: str = "cheaper") -> tuple[str | None, list[Offer], dict]:
    """Retourne (raison du ping ou None, nouvelles annonces, nouvel état).

    - premier passage : on mémorise l'état sans pinger
    - mode "cheaper" : ping quand une annonce plus basse que la précédente apparaît
      (ou quand l'item est remis en vente après rupture)
    - mode "all" : ping pour chaque nouvelle annonce (nécessite le détail des annonces)
    - max_price : seules les annonces <= max_price déclenchent un ping
    """
    ask = snap.ask
    sigs = sorted({o.signature for o in snap.offers})[:1000]
    new_state = {"last_ask": ask, "count": snap.count, "seen": sigs}
    if state is None:
        return None, [], new_state

    def ok(price: float | None) -> bool:
        return price is not None and (max_price is None or price <= max_price)

    if mode == "all" and snap.offers and state.get("seen") is not None:
        seen = set(state["seen"])
        fresh = [o for o in snap.offers if o.signature not in seen and ok(o.price)]
        if fresh:
            return "nouvelle annonce", fresh, new_state
        return None, [], new_state

    last = state.get("last_ask")
    if not ok(ask):
        return None, [], new_state
    if last is None:
        return "de nouveau en vente", [o for o in snap.offers if ok(o.price)][:5], new_state
    if ask < last - 1e-9:
        fresh = [o for o in snap.offers if o.price < last - 1e-9 and ok(o.price)][:5]
        return "nouvelle annonce moins chère", fresh, new_state
    return None, [], new_state


# --- Formatage -------------------------------------------------------------------------
def fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) >= 100 or float(x).is_integer():
        s = f"{x:,.0f}"
    else:
        s = f"{x:,.2f}"
    return s.replace(",", " ").replace(".", ",") + " $"
