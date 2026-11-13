"""What one seat may know, as JSON and as text for a language model.

`Game.view` is the debugging view: it holds the other players' current boards and every
combat of the round. `public_view` keeps only what the seat may know: the warband it saw
when it last fought each opponent, the public lobby (heroes, Health, tiers, tribes of the
last warband) and its own combats.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

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


def own_combats(g, p: int, detail: bool = False) -> list[dict]:
    """The seat's combats of the last round: winner and damage, and with `detail` what the
    player watched: both warbands at the start and the combat's events (the engine records
    them when `record_log` is on)."""
    if hasattr(g, "public_view"):
        cs = json.loads(g.public_view(p, True)).get("combats", [])
    else:
        cs = json.loads(g.view(p)).get("combats", [])
    out = []
    for c in cs:
        if p not in (c.get("a"), c.get("b")):
            continue
        r = {k: c.get(k) for k in ("turn", "a", "b", "winner", "damage", "truncated")}
        frames = c.get("frames") or []
        if detail and frames:
            side = 0 if c["a"] == p else 1
            r["start_me"] = frames[0]["boards"][side]
            r["start_opp"] = frames[0]["boards"][1 - side]
            r["log"] = [line for f in frames for line in f.get("log", [])]
        out.append(r)
    return out


@lru_cache(maxsize=1)
def _catalog() -> dict:
    from taverngym import _core
    return {c["id"]: c for c in json.loads(_core.catalog_json())}


def _event(line: str, seat: int) -> str:
    """An engine log line in the player's words."""
    line = re.sub(r"^\[t\d+\]\s*", "", line)
    line = re.sub(rf"\bp{seat}'s\b", "your", line)
    line = re.sub(rf"\bp{seat}\b", "you", line)
    line = re.sub(r"\byou (gets|summons|gains|discovers|upgrades|plays|buys|sells)\b",
                  lambda m: "you " + m.group(1)[:-1], line)
    line = line.replace("you start of turn:", "start of turn:")
    return re.sub(r"\bp(\d)\b", r"player \1", line)


def compact(c: dict) -> str:
    """A minion in a few words: name, stats, keywords."""
    kws = [kw(k) for k in c.get("keywords") or [] if k != "MAGNETIC"]
    return (("Golden " if c.get("golden") else "") + f"{c.get('name', '?')} "
            f"{c.get('atk', 0)}/{c.get('hp', 0)}" + (f" [{', '.join(kws)}]" if kws else ""))


KIND = {"tavern_spell": "Tavern spell", "spell": "spell", "trinket_lesser": "lesser trinket",
        "trinket_greater": "greater trinket", "hero_power": "Hero Power", "dark_gift": "Dark Gift",
        "quest": "quest", "quest_reward": "quest reward", "hero": "hero"}


def kw(k: str) -> str:
    return KW.get(k, k.replace("_", " ").title())


def _text(c: dict) -> str:
    t = (c.get("golden_text") or {}).get("text") if c.get("golden") and not c.get(
        "live_text") else None
    return " ".join((t or c.get("text") or "").split())


def is_minion(c: dict) -> bool:
    """Hand, Tavern and Discover items carry `type`; warband minions (`minion_json`) carry an
    entity id and no type; static cards (trinkets, heroes, Hero Powers) carry `kind`."""
    if c.get("type"):
        return c["type"] == "minion"
    return "eid" in c or c.get("kind") == "minion"


def card(c: dict, i: int | None = None, price=False) -> str:
    """One card on a line: name, tribe, tier, stats, keywords, text."""
    head = f"{i}. " if i is not None else ""
    if not is_minion(c):
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
        gifts = [a for a in att if a.get("kind") == "dark_gift"]
        mags = [a for a in att if a.get("kind") != "dark_gift"]
        if mags:
            s += " (magnetized: " + ", ".join(a.get("name", "?") for a in mags) + ")"
        for a in gifts:
            s += f" [Dark Gift {a.get('name', '?')}: {_text(a)}]"
        if price and c.get("price") is not None and c.get("price") != 3:
            s += f" (costs {c['price']})"
        if c.get("triple_need") and c.get("owned"):
            s += f" (you own {c['owned']}/{c['triple_need']} for a triple)"
    if c.get("frozen"):
        s += " (frozen)"
    if c.get("temporary"):
        s += " (temporary: leaves your hand at the end of the turn)"
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
    for k, o in enumerate(pend.get("options") or []):
        if kind == "hero":
            hp = o.get("hero_power") or {}
            s = f"{o.get('name', '?')} (Health {o.get('hp', '?')}, Armor {o.get('armor', 0)})"
            if hp:
                s += f"; Hero Power {hp.get('name', '?')} (cost {hp.get('cost', 0)}): " \
                     f"{_text(hp)}"
        elif kind == "choose_one":
            modes = re.split(r";\s*or\s+", re.sub(r"^.*?Choose One\s*-\s*", "", _text(o)))
            s = f"{o.get('name', '?')}, mode {k + 1}: " + (
                modes[k] if len(modes) == len(pend.get("options") or []) else _text(o))
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


# What a player learns from the game's help and experience, for the terms in its view.
GLOSSARY = {
    "Battlecry": "triggers when you play the card from your hand.",
    "Deathrattle": "triggers when the minion dies.",
    "Rally": "triggers whenever this minion attacks.",
    "Avenge": "Avenge (X) triggers after X friendly minions die, then counts again.",
    "Start of Combat": "triggers when combat begins, before the first attack.",
    "Activate": "Activate (N): pay N Gold during your turn to trigger it, once per turn per "
                "minion (the legal actions list it when you can).",
    "Magnetic": "can be played onto a friendly Mech (magnetize): it merges into it, adding "
                "its stats and effects.",
    "Choose One": "when played, pick one of the two effects.",
    "Discover": "choose one of several offered cards to get.",
    "Tavern spell": "a spell bought from the Tavern; it goes to your hand and is cast from "
                    "there.",
    "Blood Gem": "a Quilboar spell: give a minion +1/+1 (improved by some cards).",
    "Spellcraft": "at the start of your turn, get a temporary spell (used this turn or lost) "
                  "from this minion.",
    "Dark Gift": "a bonus effect attached to a minion; it stays with that minion.",
    "Triple": "three copies of a minion merge into a Golden one (double stats); playing a "
              "Golden minion gives a Triple Reward: Discover a minion from one Tier higher.",
    "Free Refresh": "a refresh that costs no Gold.",
    "Trinket": "a permanent effect for your hero, bought from an offer on fixed turns.",
}
_GLOSS_KEYS = {"Battlecry": r"Battlecr", "Deathrattle": r"Deathrattle", "Rally": r"\bRally",
               "Avenge": r"\bAvenge", "Start of Combat": r"Start of Combat",
               "Activate": r"\bActivate", "Magnetic": r"Magnetic|magnetiz",
               "Choose One": r"Choose One", "Discover": r"\bDiscover",
               "Tavern spell": r"Tavern spell", "Blood Gem": r"Blood Gem",
               "Spellcraft": r"Spellcraft", "Dark Gift": r"Dark Gift", "Triple": r"Golden|triple",
               "Free Refresh": r"free Refresh", "Trinket": r"[Tt]rinket"}


def glossary(text: str) -> list[str]:
    """Definitions of the game terms that appear in `text`."""
    return [f"- {k}: {GLOSSARY[k]}" for k, pat in _GLOSS_KEYS.items() if re.search(pat, text)]


@lru_cache(maxsize=1)
def _names() -> tuple[dict, dict]:
    """(card name -> normal card, family word -> normal cards whose name ends with it)."""
    by_name, family = {}, {}
    for c in _catalog().values():
        if c.get("kind") in ("hero", "hero_power", "quest", "quest_reward") or not c.get("name"):
            continue
        if c.get("normal_id") and c["normal_id"] != c["id"]:
            continue  # golden versions
        prev = by_name.get(c["name"])
        if prev is None or (c.get("in_pool") and not prev.get("in_pool")):
            by_name[c["name"]] = c
    for c in by_name.values():
        words = c["name"].split()
        if len(words) > 1 and c.get("kind") == "minion":
            family.setdefault(words[-1], []).append(c)
    family = {w: cs for w, cs in family.items() if len(cs) >= 2 and w not in by_name}
    return by_name, family


def _ref(c: dict) -> str:
    if c.get("kind") != "minion":
        return f"{c['name']} (spell) - {_text(c)}"
    tribe = "/".join(r.title() for r in c.get("races") or []) or "no tribe"
    return f"{c['name']} ({tribe}, tier {c.get('tier')}) {c.get('atk')}/{c.get('hp')} - {_text(c)}"


def references(text: str, shown: set[str], limit: int = 14) -> list[str]:
    """The cards that card texts in `text` name but that are not shown themselves: a card
    by its name, or a family ("a random Chromadrake") by its members."""
    by_name, family = _names()
    out, seen = [], set()
    tribes = {"Beast", "Demon", "Dragon", "Elemental", "Mech", "Murloc", "Naga", "Pirate",
              "Quilboar", "Undead", "Aberration", "Minion", "Spell"}
    for w, cs in family.items():
        if w in tribes or w in seen:
            continue
        if re.search(rf"\b(?:random|a|an|another|\d+|two|three) (?:\w+ )?{re.escape(w)}s?\b",
                     text):
            seen.add(w)
            out.append(f"- {w} (one of): " + "; ".join(_ref(c) for c in cs))
    for name, c in by_name.items():
        if len(out) >= limit:
            break
        if name in shown or len(name) < 4 or name in seen or name in GLOSSARY:
            continue
        if re.search(rf"\b{re.escape(name)}s?\b", text):
            seen.add(name)
            out.append(f"- {_ref(c)}")
    return out[:limit]


def lobby_pool(races: list[str]) -> dict:
    """The Tavern's minions and spells in a lobby with these tribes, by tier."""
    lobby = set(races)
    tiers: dict[int, list[dict]] = {}
    for c in _names()[0].values():
        if not c.get("in_pool") or not c.get("implemented"):
            continue
        if c.get("kind") == "minion":
            rs = set(c.get("races") or [])
            if rs and "ALL" not in rs and not rs & lobby:
                continue
        elif c.get("kind") != "tavern_spell":
            continue
        tiers.setdefault(c.get("tier") or 0, []).append(c)
    return {t: sorted(cs, key=lambda c: (c.get("kind") != "minion", c["name"]))
            for t, cs in sorted(tiers.items())}


def pool_text(races: list[str]) -> list[str]:
    lines = ["Cards in this lobby's Tavern (minions with tribe and stats, then spells):"]
    for t, cs in lobby_pool(races).items():
        ms = [f"{c['name']} ({'/'.join(r.title() for r in c['races']) or '-'} "
              f"{c['atk']}/{c['hp']})" for c in cs if c.get("kind") == "minion"]
        sp = [c["name"] for c in cs if c.get("kind") != "minion"]
        lines.append(f"  Tier {t}: " + "; ".join(ms) + (" | spells: " + "; ".join(sp)
                                                          if sp else ""))
    return lines


def _card_texts(v: dict) -> list[str]:
    """The texts of the cards in the seat's view (not their names)."""
    me, out = v["me"], []

    def add(c):
        if isinstance(c, dict):
            out.append(_text(c))
            for a in c.get("attached") or []:
                out.append(_text(a))
    for z in ("board", "hand", "shop", "trinkets", "hero_powers", "rewards", "secrets"):
        for c in me.get(z) or []:
            add(c)
    for o in (me.get("pending") or {}).get("options") or []:
        add(o)
        if isinstance(o, dict):
            add(o.get("hero_power"))
    for q in me.get("quests") or []:
        add(q.get("quest"))
        add(q.get("reward"))
    for o in v.get("opponents") or []:
        for c in o.get("seen_board") or []:
            add(c)
    return out


def render(v: dict, combats: list[dict] | None = None, reference: bool = True,
           pool: bool = False) -> str:
    """The seat's view as text; with `reference`, also what a player knows from the game:
    the mechanics and the cards its texts mention, and with `pool` the lobby's card pool."""
    body = _render(v, combats)
    if not reference or v.get("phase") == "hero_select":
        return body
    me = v["me"]
    shown = {c.get("name") for z in ("board", "hand", "shop") for c in me[z]}
    texts = "\n".join(_card_texts(v))
    extra = []
    refs = references(texts, shown)
    if refs:
        extra += ["Cards mentioned above:"] + refs
    gl = glossary(body)
    if gl:
        extra += ["Game terms:"] + gl
    if pool:
        extra += pool_text(v.get("lobby_races") or [])
    return body + "\n" + "\n".join(extra)


def _render(v: dict, combats: list[dict] | None = None) -> str:
    """The seat's view as text."""
    me = v["me"]
    seat = v.get("seat", 0)
    tribes = ", ".join(r.title() for r in v.get("lobby_races", []))
    lines = [(f"Turn {v['turn']} ({v['phase'].replace('_', ' ')}). "
              f"Players alive: {v['alive']}. Tribes in this lobby: {tribes}.")]
    if v.get("paradox"):
        lines.append(f"This lobby's Paradox: {v['paradox'].get('name')} - {_text(v['paradox'])}")
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
            tiers = "/".join(str(t) for t in dg.get("tiers") or [])
            nxt = "/".join(str(t) for t in dg.get("next_tiers") or [])
            lines.append(f"Dark Gift available (cost {dg.get('cost')}, used {dg.get('uses')}/"
                         f"{dg.get('max_uses')})" + (f": offers tier {tiers} minions now" if tiers
                                                    else "")
                         + (f", tier {nxt} next turn" if nxt else "") + ".")
        dt = me.get("deity")
        if dt:
            kwd = [w for w, on in (("Golden", dt.get("golden")), ("Reborn", dt.get("reborn"))) if on]
            lines.append(f"Your Deity: {(dt.get('card') or {}).get('name', '?')} {dt.get('atk')}/"
                         f"{dt.get('hp')}" + (f" [{', '.join(kwd)}]" if kwd else "")
                         + f" - {_text(dt.get('card') or {})}")
        for e in me.get("effects") or []:
            lines.append(f"Active effect: {(e.get('card') or {}).get('name', '?')}"
                         + (f" x{e['stacks']}" if (e.get("stacks") or 1) > 1 else "")
                         + f" - {_text(e.get('card') or {})}")
        for e in me.get("scheduled") or []:
            lines.append(f"Coming up: {(e.get('card') or {}).get('name', '?')} in "
                         f"{e.get('turns_left')} turn(s)" + (" if you win" if e.get("if_won")
                                                             else "")
                         + f" - {_text(e.get('card') or {})}")
        bb = me.get("buddy_button")
        if bb and bb.get("available"):
            lines.append(f"Buddy available: {(bb.get('card') or {}).get('name')} for "
                         f"{bb.get('price')} Gold - {_text(bb.get('card') or {})}")
        # only what happened to this player (the others' events are not on its screen)
        ev = [_event(x, seat) for x in me.get("turn_events") or []
              if re.search(rf"\bp{seat}\b", x)]
        if ev:
            lines.append("At the start of this turn: " + "; ".join(ev) + ".")
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
            hp = _catalog().get((o.get("hero") or {}).get("hero_power_id") or "")
            if hp:
                lines.append(f"    Hero Power {hp.get('name')}: {_text(hp)}")
            if o.get("trinkets"):
                lines.append("    Trinkets: " + "; ".join(
                    f"{t.get('name')} - {_text(t)}" for t in o["trinkets"]))
            for q in o.get("quests") or []:
                lines.append(f"    Quest: {q['quest'].get('name')} ({q.get('progress')}/"
                             f"{q.get('goal')}), reward {q['reward'].get('name')}")
            if o.get("rewards"):
                lines.append("    Rewards: " + "; ".join(r.get("name", "?") for r in o["rewards"]))
            if o.get("seen_board"):
                if o.get("is_next_opponent"):
                    lines.append(f"    warband you saw on turn {o.get('seen_turn')}:")
                    lines += ["      " + card(c, i) for i, c in enumerate(o["seen_board"])]
                else:
                    lines.append(f"    warband you saw on turn {o.get('seen_turn')}: "
                                 + ", ".join(compact(c) for c in o["seen_board"]))
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
        if c.get("log") is not None:
            lines.append("  Their warband at the start: "
                         + (", ".join(compact(m) for m in c.get("start_opp") or []) or "empty"))
            lines.append("  Your warband at the start: "
                         + (", ".join(compact(m) for m in c.get("start_me") or []) or "empty"))
            log = [_event(x, seat) for x in c["log"]]
            if len(log) > 60:
                log = log[:60] + [f"... ({len(log) - 60} more events)"]
            lines.append("  What happened:")
            lines += ["    " + x for x in log]
    return "\n".join(lines)
