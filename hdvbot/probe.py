"""Diagnostic : appelle chaque endpoint HDV, sauvegarde le JSON brut et montre ce que le bot en comprend.

À lancer une fois avant le bot :  python -m hdvbot probe
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .api import ApiError, NGApi, unwrap
from .config import Config
from .fields import normalize_item, normalize_trade
from .logic import fmt_money


def _shape(x: Any, depth: int = 0) -> str:
    pad = "    " * depth
    if isinstance(x, dict):
        lines = []
        for k, v in list(x.items())[:25]:
            if isinstance(v, (dict, list)) and depth < 2:
                lines.append(f"{pad}{k}: {type(v).__name__}" + (f"[{len(v)}]" if isinstance(v, list) else ""))
                inner = v[0] if isinstance(v, list) and v else v
                if isinstance(inner, dict):
                    lines.append(_shape(inner, depth + 1))
            else:
                lines.append(f"{pad}{k}: {json.dumps(v, ensure_ascii=False, default=str)[:70]}")
        return "\n".join(lines)
    if isinstance(x, list):
        return f"{pad}liste de {len(x)} élément(s)" + ("\n" + _shape(x[0], depth + 1) if x else "")
    return f"{pad}{json.dumps(x, ensure_ascii=False, default=str)[:200]}"


async def run_probe(cfg: Config, item: str | None, out_dir: str = "probe_output"):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    s = cfg.server
    async with NGApi(cfg.base_url, s, cfg.api_key, cfg.request_delay, cfg.page_size) as api:
        print(f"API : {cfg.base_url} — serveur « {s} » — clé API : {'oui' if cfg.api_key else 'non'}\n")

        async def call(name: str, path: str, params: dict | None = None) -> Any:
            print("=" * 78)
            print(f"{name}  →  GET {path}" + (f"  {params}" if params else ""))
            try:
                raw = await api.request(path, params)
            except ApiError as e:
                print(f"  ✗ ERREUR : {e}\n")
                return None
            (out / f"{name}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str),
                                              encoding="utf-8")
            data, meta = unwrap(raw)
            if meta:
                print("  enveloppe / meta :")
                print(_shape(meta, 2))
            print("  données :")
            print(_shape(data, 2))
            first = data[0] if isinstance(data, list) and data else data
            print("  1er enregistrement brut :")
            print("    " + json.dumps(first, ensure_ascii=False, default=str)[:1500])
            print(f"  (JSON complet : {out / (name + '.json')})\n")
            return data

        await call("servers", "/servers")
        items = await call("items", f"/servers/{s}/hdv/items", {"limit": 5})
        if isinstance(items, list) and items:
            print("  → ce que le bot comprend des 5 premiers items :")
            for r in items[:5]:
                snap = normalize_item(r, cfg.fields) if isinstance(r, dict) else None
                if snap is None:
                    print("    ✗ item illisible (id introuvable) → voir `fields:` dans config.yaml")
                    continue
                print(f"    {snap.id:>10}  {str(snap.name)[:28]:28}  moins cher={fmt_money(snap.ask)}"
                      f"  qté={snap.ask_qty}  en vente={snap.count}  cours API={fmt_money(snap.api_ref)}")
            print()

        target = item or (cfg.watchlist[0].item if cfg.watchlist else None)
        if target is None and isinstance(items, list) and items and isinstance(items[0], dict):
            first = normalize_item(items[0], cfg.fields)
            target = first.id if first else None
        if target:
            det = await call("item_detail", f"/servers/{s}/hdv/items/{target}")
            if isinstance(det, dict):
                snap = normalize_item(det, cfg.fields, item_hint=target)
                print(f"  → compris : nom={snap.name!r} moins cher={fmt_money(snap.ask)} qté={snap.ask_qty} "
                      f"en vente={snap.count} annonces détaillées={len(snap.offers)} "
                      f"ventes incluses={len(snap.trades)} cours API={fmt_money(snap.api_ref)}")
                for o in snap.offers[:3]:
                    print(f"      annonce : {o.qty} × {fmt_money(o.price)} — {o.seller}")
                print()
            tr = await call("trades_item", f"/servers/{s}/hdv/trades", {"item": target, "limit": 5})
            if isinstance(tr, list):
                print("  → ventes comprises :")
                for r in tr[:5]:
                    t = normalize_trade(r, target, cfg.fields) if isinstance(r, dict) else None
                    print(f"      {t.qty} × {fmt_money(t.price)} @ {t.ts}" if t else "      ✗ vente illisible")
                print()
            await call("series_24h", f"/servers/{s}/hdv/items/{target}/series", {"range": "24h"})
        tg = await call("trades_global", f"/servers/{s}/hdv/trades", {"limit": 5})
        if isinstance(tg, list):
            ok = sum(1 for r in tg if isinstance(r, dict) and normalize_trade(r, None, cfg.fields))
            print(f"  → flux global : {ok}/{len(tg)} ventes lisibles avec leur item\n")

        print("Si un prix/nom/id est faux ou vide ci-dessus, ouvre le JSON correspondant dans "
              f"{out}/ et force le bon champ dans la section `fields:` de config.yaml.")
