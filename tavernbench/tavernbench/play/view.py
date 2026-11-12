"""What one seat may know, as JSON and as text for a language model.

`Game.view` is the debugging view: it holds the other players' current boards and every
combat of the round. `public_view` keeps only what the seat may know: the warband it saw
when it last fought each opponent, the public lobby (heroes, Health, tiers, tribes of the
last warband) and its own combats.
"""

from __future__ import annotations

import json
import re

KW = {
    "DIVINE_SHIELD": "Divine Shield", "TAUNT": "Taunt", "VENOMOUS": "Venomous",
    "POISONOUS": "Poisonous", "REBORN": "Reborn", "WINDFURY": "Windfury",
    "MEGA_WINDFURY": "Mega-Windfury", "CLEAVE": "Cleave", "STEALTH": "Stealth",
    "MAGNETIC": "Magnetic",
}


def public_view(g, p: int) -> dict:
    """The seat's player-visible view (no action mask, no rules config)."""
    if hasattr(g, "public_view"):
        v = json.loads(g.public_view(p, False))
    else:  # an extension built before `public_view` was exposed: drop the private parts
        v = json.loads(g.view(p))
        for o in v.get("opponents", []):
            o["board"], o["last_board"] = [], []
        v["combats"] = []
    v.pop("mask", None)
    v.pop("config", None)
    return v


def own_combats(g, p: int) -> list[dict]:
    """The seat's combats of the last round (winner and damage; no replay frames)."""
    if hasattr(g, "public_view"):
        cs = json.loads(g.public_view(p, True)).get("combats", [])
    else:
        cs = json.loads(g.view(p)).get("combats", [])
    return [{k: c.get(k) for k in ("turn", "a", "b", "winner", "damage", "truncated")}
            for c in cs if c.get("a") == p or c.get("b") == p]


KIND = {"tavern_spell": "Tavern spell", "spell": "spell", "trinket_lesser": "lesser trinket",
        "trinket_greater": "greater trinket", "hero_power": "Hero Power", "dark_gift": "Dark Gift",
        "quest": "quest", "quest_reward": "quest reward", "hero": "hero"}


def kw(k: str) -> str:
    return KW.get(k, k.replace("_", " ").title())


def _text(c: dict) -> str:
    t = (c.get("golden_text") or {}).get("text") if c.get("golden") and not c.get(
        "live_text") else None
    return " ".join((t or c.get("text") or "").split())


def card(c: dict, i: int | None = None, price=False) -> str:
    """One card on a line: name, tribe, tier, stats, keywords, text."""
    head = f"{i}. " if i is not None else ""
    if c.get("type", "minion" if c.get("kind") == "minion" else None) != "minion":
        cost = c["price"] if price and c.get("price") is not None else c.get("cost")
        label = KIND.get(c.get("kind"), "spell")
        s = f"{head}{c.get('name', '?')} ({label}" + (f", cost {cost}" if cost is not None else "") + ")"
    else:
        races = "/".join(r.title() for r in c.get("races") or []) or "no tribe"
        gold = "Golden " if c.get("golden") else ""
        s = f"{head}{gold}{c.get('name', '?')} ({races}, tier {c.get('tier', '?')}) " \
            f"{c.get('atk', 0)}/{c.get('hp', 0)}"
        kws = [kw(k) for k in c.get("keywords") or []]
        if kws:
            s += " [" + ", ".join(kws) + "]"
        att = c.get("attached") or []
        if att:
            s += " (magnetized: " + ", ".join(a.get("name", "?") for a in att) + ")"
        if price and c.get("price") is not None and c.get("price") != 3:
            s += f" (costs {c['price']})"
        if c.get("triple_need") and c.get("owned"):
            s += f" (you own {c['owned']}/{c['triple_need']} for a triple)"
    if c.get("frozen"):
        s += " (frozen)"
    t = _text(c)
    return s + (f" - {t}" if t else "")


def zone(title: str, cards: list[dict], price=False) -> list[str]:
    lines = [f"{title}:"]
    if not cards:
        lines.append("  (empty)")
    lines += ["  " + card(c, i, price) for i, c in enumerate(cards)]
    return lines


def names_by_eid(me: dict) -> dict:
    out = {}
    for zname, z in (("warband", me["board"]), ("hand", me["hand"]), ("tavern", me["shop"])):
        for i, c in enumerate(z):
            if c.get("eid") is not None:
                out[c["eid"]] = f"{c.get('name', '?')} ({zname} {i})"
    return out


def pending_options(me: dict) -> list[str]:
    """The pending choice's options in order (index k = `choose k`)."""
    pend = me.get("pending")
    if not pend:
        return []
    if pend.get("kind") == "target":
        names = names_by_eid(me)
        return [names.get(t, f"entity #{t}") for t in pend.get("targets", [])]
    out = []
    kind = pend.get("kind")
    for k, o in enumerate(pend.get("options", [])):
        if kind == "hero":
            hp = o.get("hero_power") or {}
            s = f"{o.get('name', '?')} (Health {o.get('hp', '?')}, Armor {o.get('armor', 0)})"
            if hp:
                s += f"; Hero Power {hp.get('name', '?')} (cost {hp.get('cost', 0)}): " \
                     f"{_text(hp)}"
        elif kind == "choose_one":
            modes = re.split(r";\s*or\s+", re.sub(r"^.*?Choose One\s*-\s*", "", _text(o)))
            s = f"{o.get('name', '?')}, mode {k + 1}: " + (
                modes[k] if len(modes) == len(pend["options"]) else _text(o))
        elif kind == "trinket":
            s = card(o)  # its cost is what taking it costs this player now
        elif kind == "quest":
            r = o.get("reward") or {}
            s = f"{o.get('name', '?')}: {_text(o)} (goal {o.get('goal')}); reward " \
                f"{r.get('name', '?')}: {_text(r)}"
        elif kind == "timewarp":
            s = card(o) + f" (costs {o.get('chronum_cost')} Chronum)"
        else:
            s = card(o)
        out.append(s)
    if kind == "timewarp":
        out.append("leave the Timewarped Tavern")
    return out


def render(v: dict, combats: list[dict] | None = None) -> str:
    """The seat's view as text."""
    me = v["me"]
    seat = v.get("seat", 0)
    tribes = ", ".join(r.title() for r in v.get("lobby_races", []))
    lines = [(f"Turn {v['turn']} ({v['phase'].replace('_', ' ')}). "
              f"Players alive: {v['alive']}. Tribes in this lobby: {tribes}.")]
    if v.get("phase") != "hero_select":
        hero = (me.get("hero") or {}).get("name", "?")
        lines.append(
            f"You: {hero}, Health {me['health']}" + (f" + {me['armor']} Armor" if me["armor"]
                                                     else "") +
            f", Tavern tier {me['tier']}, Gold {me['gold']}/{me['max_gold']}"
            + (f", win streak {me['win_streak']}" if me.get("win_streak") else "") + ".")
        prices = [f"buy a minion {me['minion_price']}", f"refresh {me['refresh_price']}"
                  + (f" ({me['free_refreshes']} free)" if me.get("free_refreshes") else "")]
        if me.get("can_upgrade_tier"):
            prices.append(f"upgrade the Tavern {me['upgrade_price']}")
        lines.append("Prices: " + ", ".join(prices) + ". Selling a minion gives 1 Gold."
                     + (" The Tavern is frozen." if me.get("frozen") else ""))
        for hp in me.get("hero_powers") or []:
            if hp.get("passive"):
                state = "passive"
            else:
                state = f"cost {hp.get('price')}, " + ("usable now" if hp.get("usable")
                                                        else "not usable now")
            lines.append(f"Hero Power {hp.get('slot', 0)}: {hp.get('name', '?')} ({state}) - "
                         f"{_text(hp)}")
        for title, key in (("Trinkets", "trinkets"), ("Rewards", "rewards"),
                           ("Secrets", "secrets")):
            if me.get(key):
                lines.append(f"{title}: " + "; ".join(card(c) for c in me[key]))
        for q in me.get("quests") or []:
            lines.append(f"Quest: {q['quest'].get('name')} - {_text(q['quest'])} "
                         f"(progress {q.get('progress')}/{q.get('goal')}); reward "
                         f"{q['reward'].get('name')}: {_text(q['reward'])}")
        dg = me.get("dark_gift") or {}
        if dg.get("available"):
            lines.append(f"Dark Gift available (cost {dg.get('cost')}, used {dg.get('uses')}/"
                         f"{dg.get('max_uses')}).")
        lines += zone("Your warband (left to right; attacks in this order)", me["board"])
        lines += zone(f"Your hand ({len(me['hand'])}/{me.get('hand_limit', 10)})", me["hand"])
        lines += zone("Tavern", me["shop"], price=True)
    pend = me.get("pending")
    if pend:
        src = (pend.get("source") or {}).get("name")
        lines.append(f"PENDING CHOICE ({pend.get('kind')}" + (f", from {src}" if src else "")
                     + "): you must choose one of")
        lines += [f"  {k}: {s}" for k, s in enumerate(pending_options(me))]
        if me.get("cancellable"):
            lines.append("  (or back out with the cancel action)")
    if v.get("phase") != "hero_select":
        lines.append("Lobby (other players):")
        for o in v.get("opponents", []):
            if not o.get("alive"):
                lines.append(f"  player {o['id']}: eliminated (place {o.get('placement')})")
                continue
            hero = (o.get("hero") or {}).get("name", "?")
            tags = []
            if o.get("is_next_opponent"):
                tags.append("NEXT OPPONENT")
            if o.get("was_last_opponent"):
                tags.append("last opponent")
            lw = o.get("last_warband") or {}
            tribe = ", ".join(f"{c['count']} {c['race'].title()}" for c in lw.get("counts", []))
            s = (f"  player {o['id']}: {hero}, Health {o['health']}"
                 + (f"+{o['armor']}" if o.get("armor") else "") + f", tier {o['tier']}"
                 + (f", win streak {o['win_streak']}" if o.get("win_streak") else "")
                 + (f", last warband {lw.get('size')} minions ({tribe})" if lw.get("size")
                    else ""))
            if tags:
                s += " [" + ", ".join(tags) + "]"
            lines.append(s)
            if o.get("is_next_opponent") or o.get("was_last_opponent"):
                if o.get("seen_board"):
                    lines.append(f"    warband you saw on turn {o.get('seen_turn')}:")
                    lines += ["      " + card(c, i) for i, c in enumerate(o["seen_board"])]
                elif o.get("is_next_opponent"):
                    lines.append("    (you have not fought this player yet)")
        if me.get("next_is_ghost"):
            lines.append("  Your next opponent is a ghost of an eliminated player.")
    for c in combats or []:
        # `winner` is a side: 0 = a won, 1 = b won, 2 = tie (b is None against a ghost).
        side = 0 if c["a"] == seat else 1
        other = c["b"] if side == 0 else c["a"]
        other = "a ghost" if other is None else f"player {other}"
        if c.get("winner") == 2:
            res = "tie"
        elif c.get("winner") == side:
            res = f"you won and dealt {c.get('damage')} damage"
        else:
            res = f"you lost and took {c.get('damage')} damage"
        lines.append(f"Last combat (turn {c.get('turn')}) against {other}: {res}.")
    return "\n".join(lines)
