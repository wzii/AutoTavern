"""The legal actions of a seat, each with a description a language model can read.

Action indices are TavernGym's (see docs/TAVERNGYM.md); an index always refers to the state
it was listed in (positions shift after a buy, sell or play).
"""

from __future__ import annotations

from . import view as V

END, REFRESH, FREEZE, UPGRADE, HERO_POWER, DARK_GIFT, CANCEL = 0, 1, 2, 3, 4, 6, 7
BUY, SELL, ACTIVATE, CHOOSE, PLAY, MAGNETIZE, MOVE, N_ACTIONS = 8, 16, 23, 30, 46, 126, 196, 245
N_POS, N_BOARD = 8, 7


def legal(g, p: int) -> list[int]:
    return [i for i, b in enumerate(g.legal_mask(p)) if b]


def _name(z: list[dict], i: int) -> str:
    return z[i].get("name", "?") if i < len(z) else "?"


def describe(a: int, v: dict) -> str:
    me = v["me"]
    board, hand, shop = me["board"], me["hand"], me["shop"]
    if a == END:
        return "end the turn (go to combat)"
    if a == REFRESH:
        return f"refresh the Tavern ({me.get('refresh_price')} Gold)"
    if a == FREEZE:
        return "unfreeze the Tavern" if me.get("frozen") else "freeze the Tavern"
    if a == UPGRADE:
        return f"upgrade the Tavern to tier {me['tier'] + 1} ({me.get('upgrade_price')} Gold)"
    if a in (HERO_POWER, HERO_POWER + 1):
        slot = a - HERO_POWER
        hp = next((h for h in me.get("hero_powers") or [] if h.get("slot") == slot), {})
        return f"use Hero Power {hp.get('name', slot)} ({hp.get('price', '?')} Gold)"
    if a == DARK_GIFT:
        return f"take a Dark Gift ({(me.get('dark_gift') or {}).get('cost', '?')} Gold)"
    if a == CANCEL:
        return "back out of the current target choice"
    if BUY <= a < SELL:
        i = a - BUY
        price = shop[i].get("price") if i < len(shop) else None
        return f"buy Tavern {i}: {_name(shop, i)}" + (f" ({price} Gold)" if price is not None
                                                     else "")
    if SELL <= a < ACTIVATE:
        return f"sell warband {a - SELL}: {_name(board, a - SELL)}"
    if ACTIVATE <= a < CHOOSE:
        return f"activate warband {a - ACTIVATE}: {_name(board, a - ACTIVATE)}"
    if CHOOSE <= a < PLAY:
        k = a - CHOOSE
        opts = V.pending_options(me)
        return f"choose {k}: " + (opts[k] if k < len(opts) else "?")
    if PLAY <= a < MAGNETIZE:
        h, pos = divmod(a - PLAY, N_POS)
        c = hand[h] if h < len(hand) else {}
        if c.get("type") == "spell":
            return f"play hand {h}: {c.get('name', '?')} (spell)"
        return f"play hand {h}: {c.get('name', '?')} into warband position {pos}"
    if MAGNETIZE <= a < MOVE:
        h, j = divmod(a - MAGNETIZE, N_BOARD)
        return f"magnetize hand {h}: {_name(hand, h)} onto warband {j}: {_name(board, j)}"
    if MOVE <= a < N_ACTIONS:
        i, j = divmod(a - MOVE, N_BOARD)
        return f"move warband {i}: {_name(board, i)} to position {j}"
    return f"action {a}"


def listing(g, p: int, v: dict) -> tuple[list[int], str]:
    """(legal indices, one '[index] description' line each). Spells that are legal at every
    position are listed once (position 0)."""
    acts = legal(g, p)
    me = v["me"]
    keep = []
    for a in acts:
        if PLAY <= a < MAGNETIZE:
            h, pos = divmod(a - PLAY, N_POS)
            if pos and h < len(me["hand"]) and me["hand"][h].get("type") == "spell" \
                    and PLAY + h * N_POS in acts:
                continue
        keep.append(a)
    return acts, "\n".join(f"[{a}] {describe(a, v)}" for a in keep)
