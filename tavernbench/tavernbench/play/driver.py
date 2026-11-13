"""Play complete TavernGym games with an agent in one seat and the scripted bots in the rest.

At every decision point of its seat the agent gets a `Decision` (the visible state as text,
the labelled legal actions, the game itself for tools that work on copies) and returns a plan:
a list of action indices. The driver executes the plan in order and asks again as soon as
  - an action is illegal (the agent is told why; after `retries` failures the bot decides),
  - an action reveals something new (a refresh, a pending choice, a card that was not bought
    appearing in hand or in the Tavern), or
  - the turn ends.
An agent that does not end its turn within `turn_cap` actions has the turn ended for it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from taverngym import _core

from . import actions as A
from . import view as V


@dataclass
class Decision:
    game: object            # the live game: agents only read it or work on copies
    seat: int
    view: dict
    state_text: str
    legal: list[int]
    actions_text: str
    turn_log: list[str]     # this turn's executed actions, oldest first
    feedback: str = ""      # why the previous plan was cut short or rejected
    notes: str = ""         # the agent's memo carried between decisions


@dataclass
class Plan:
    actions: list[int]
    notes: str | None = None    # replaces the memo when given
    info: dict = field(default_factory=dict)  # tokens, tool calls, raw text... (logged)


def unplayed(g, seat: int, v: dict) -> list[tuple[str, str]]:
    """Minions in hand that could be played into the warband now: (key, text) pairs."""
    me = v["me"]
    if len(me["board"]) >= 7:
        return []
    legal = set(A.legal(g, seat))
    return [(f"play:{c.get('hid')}", f"hand {h}: {c.get('name', '?')}")
            for h, c in enumerate(me["hand"]) if c.get("type") == "minion"
            and any(A.PLAY + h * A.N_POS + p in legal for p in range(A.N_POS))]


def unspent(g, seat: int, v: dict) -> list[tuple[str, str]]:
    """What the seat's remaining Gold could still do this turn: (key, text) pairs."""
    me = v["me"]
    if me["gold"] <= 0:
        return []
    legal = set(A.legal(g, seat))
    can = []
    buys = [me["shop"][a - A.BUY] for a in sorted(legal) if A.BUY <= a < A.SELL
            and a - A.BUY < len(me["shop"])]
    if buys:
        can.append(("buy", "buy (" + ", ".join(f"{c.get('name')} {c.get('price')}"
                                               for c in buys[:4]) + ")"))
    for a in sorted(legal):
        if A.ACTIVATE <= a < A.CHOOSE and a - A.ACTIVATE < len(me["board"]):
            m = me["board"][a - A.ACTIVATE]
            text = f"activate {m.get('name')} ({m.get('activate_cost', '?')})"
            can.append((f"activate:{m.get('eid')}", text))
    if A.UPGRADE in legal:
        can.append(("upgrade", f"upgrade the Tavern ({me.get('upgrade_price')})"))
    for h in me.get("hero_powers") or []:
        if A.HERO_POWER + h.get("slot", 0) in legal and h.get("usable") \
                and not h.get("passive") and (h.get("price") or 0) > 0:
            can.append((f"power:{h.get('slot', 0)}", f"use {h.get('name')} ({h.get('price')})"))
    if A.DARK_GIFT in legal:
        can.append(("dark_gift", "take a Dark Gift"))
    if A.REFRESH in legal:
        can.append(("refresh", f"refresh ({me.get('refresh_price')})"))
    return can


def end_check(g, seat: int, v: dict) -> list[tuple[str, str]]:
    """Reasons to ask before ending the turn, as (key, text): minions left in hand, Gold
    left to spend. Unspent Gold is lost and minions in hand do not fight."""
    out = []
    left = unplayed(g, seat, v)
    if left:
        out.append(("|".join(k for k, _ in left), "minions in hand could still be played ("
                    + ", ".join(t for _, t in left) + "); only minions in the warband fight"))
    gold = unspent(g, seat, v)
    if gold:
        out.append(("|".join(k for k, _ in gold), f"you still have {v['me']['gold']} Gold that "
                    "could " + ", ".join(t for _, t in gold) + " (unspent Gold is lost)"))
    return out


class Snapshot:
    """The state a plan was written against: indices of later actions in the plan mean what
    the listed actions meant then (`plans="snapshot"`), and the driver follows each card by
    its identity as positions change. Hand positions past the end refer to the cards that join
    the hand during the plan (the first card bought is hand position len(hand))."""

    def __init__(self, me: dict):
        self.board = [m.get("eid") for m in me["board"]]
        self.hand = [h.get("hid") for h in me["hand"]]
        self.shop = [s.get("sid") for s in me["shop"]]
        self.spell = [h.get("type") == "spell" for h in me["hand"]]
        self.added: list = []  # hand ids that joined the hand during the plan, in order

    def note_hand(self, me: dict):
        for h in me["hand"]:
            if h.get("hid") not in self.hand and h.get("hid") not in self.added:
                self.added.append(h.get("hid"))

    def remap(self, a: int, me: dict) -> int | None:
        """`a` as written against the snapshot, as an index in the state `me` (None when the
        card it names is gone)."""
        board = [m.get("eid") for m in me["board"]]
        hand = [h.get("hid") for h in me["hand"]]
        shop = [s.get("sid") for s in me["shop"]]

        def at(lst, x):
            return lst.index(x) if x is not None and x in lst else None

        def hand_now(h):
            if h < len(self.hand):
                return at(hand, self.hand[h])
            k = h - len(self.hand)
            return at(hand, self.added[k]) if k < len(self.added) else None

        def slot(p):  # a warband position: before the minion that was there, else the end
            if p < len(self.board):
                return at(board, self.board[p])
            return len(board)

        if a < A.BUY or A.CHOOSE <= a < A.PLAY:
            return a
        if a < A.SELL:
            i = a - A.BUY
            j = at(shop, self.shop[i]) if i < len(self.shop) else None
            return None if j is None else A.BUY + j
        if a < A.CHOOSE:
            base = A.SELL if a < A.ACTIVATE else A.ACTIVATE
            i = a - base
            j = at(board, self.board[i]) if i < len(self.board) else None
            return None if j is None else base + j
        if a < A.MAGNETIZE:
            h, p = divmod(a - A.PLAY, A.N_POS)
            hh = hand_now(h)
            if hh is None:
                return None
            spell = me["hand"][hh].get("type") == "spell"
            pp = p if spell else slot(p)
            return None if pp is None else A.PLAY + hh * A.N_POS + pp
        if a < A.MOVE:
            h, j = divmod(a - A.MAGNETIZE, A.N_BOARD)
            hh = hand_now(h)
            jj = at(board, self.board[j]) if j < len(self.board) else None
            return None if hh is None or jj is None else A.MAGNETIZE + hh * A.N_BOARD + jj
        i, j = divmod(a - A.MOVE, A.N_BOARD)
        ii = at(board, self.board[i]) if i < len(self.board) else None
        jj = at(board, self.board[j]) if j < len(self.board) else len(board) - 1
        return None if ii is None or jj is None else A.MOVE + ii * A.N_BOARD + jj


def _ids(me: dict) -> tuple[set, set]:
    return ({c.get("hid") for c in me["hand"]}, {c.get("sid") for c in me["shop"]})


class Match:
    def __init__(self, agent, seed: int, seat: int = 0, config: dict | None = None,
                 turn_cap: int = 40, retries: int = 2, max_plan: int = 10, trace=None,
                 record_combats=False, end_guard=True, plans="snapshot"):
        self.agent, self.seed, self.seat = agent, seed, seat
        self.config_json = json.dumps(config) if config else None
        self.turn_cap, self.retries, self.max_plan = turn_cap, retries, max_plan
        self.trace = trace  # a callable taking one dict per decision, or None
        self.stats = {"decisions": 0, "actions": 0, "illegal": 0, "fallbacks": 0,
                      "forced_ends": 0, "input_tokens": 0, "output_tokens": 0,
                      "tool_calls": 0, "errors": 0, "cost_usd": 0.0}
        self._feedback = ""  # an illegal action that cut the last plan short
        self.record_combats = record_combats
        # Ask before End Turn with minions left to play or Gold left to spend: True asks again
        # whenever a new reason appears in the turn, "once" (earlier runs) once per turn.
        self.end_guard = end_guard
        self._guard_turn, self._guard_seen = -1, set()  # reasons already put to the agent
        # "snapshot": every index of a plan means what the listed actions meant when the agent
        # wrote it; "sequential" (runs before this option): later indices are read in the
        # state after the earlier actions.
        self.plans = plans
        self.combats: list[dict] = []  # the agent's combats with replay frames (if recorded)
        self.game = None

    def _decision(self, g, turn_log, feedback, notes) -> Decision:
        v = V.public_view(g, self.seat)
        legal, text = A.listing(g, self.seat, v)
        # The result of the last combat at every decision; what happened in it (both warbands
        # and the events, as a player watches it) at the first decision of the turn.
        combats = V.own_combats(g, self.seat, detail=not turn_log) \
            if v["phase"] == "recruit" else []
        # The lobby's card pool once per turn, at its first decision.
        state = V.render(v, combats, pool=not turn_log and v["phase"] == "recruit")
        return Decision(g, self.seat, v, state, legal, text, list(turn_log), feedback, notes)

    def _fallback(self, g) -> int:
        self.stats["fallbacks"] += 1
        return g.bot_action(self.seat)

    def _agent_turn(self, g, turn_log: list[str], notes: list[str]):
        """One decision of the agent's seat: ask, execute the plan until it must ask again."""
        feedback, self._feedback = self._feedback, ""
        for attempt in range(self.retries + 1):
            d = self._decision(g, turn_log, feedback, notes[0])
            t0 = time.time()
            try:
                plan = self.agent.decide(d)
            except Exception as e:  # noqa: BLE001 - logged; the bot decides instead
                self.stats["errors"] += 1
                plan = Plan([], info={"error": f"{type(e).__name__}: {str(e)[:200]}"})
            self.stats["decisions"] += 1
            for k in ("input_tokens", "output_tokens", "tool_calls"):
                self.stats[k] += int(plan.info.get(k, 0) or 0)
            self.stats["cost_usd"] = round(self.stats["cost_usd"]
                                           + float(plan.info.get("cost_usd", 0) or 0), 4)
            if plan.notes is not None:
                notes[0] = plan.notes[:2000]
            rec = {"seat": self.seat, "turn": g.turn, "phase": d.view["phase"],
                   "attempt": attempt, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "state": d.state_text, "feedback": d.feedback, "plan": plan.actions,
                   "notes": plan.notes, "info": plan.info,
                   "latency_s": round(time.time() - t0, 2)}
            n0 = len(turn_log)
            done, feedback = self._execute(g, plan.actions[:self.max_plan], turn_log)
            rec["executed"], rec["stop"], rec["did"] = done, feedback, turn_log[n0:]
            if self.trace:
                self.trace(rec)
            if done:
                self._feedback = feedback  # told to the agent at its next decision
                return
            if not plan.actions and "error" in plan.info:
                break
        a = self._fallback(g)  # the agent failed to give a legal first action
        turn_log.append(f"(bot) {A.describe(a, V.public_view(g, self.seat))}")
        g.step(self.seat, a)
        self.stats["actions"] += 1

    def _execute(self, g, plan: list[int], turn_log: list[str]) -> tuple[int, str]:
        """Execute `plan`; returns (actions executed, why it stopped)."""
        done = 0
        if not plan:
            return 0, "the plan was empty or could not be read"
        snap = Snapshot(V.public_view(g, self.seat)["me"]) if self.plans == "snapshot" else None
        for k, a in enumerate(plan):
            if not g.needs_action(self.seat):
                return done, ""
            v = V.public_view(g, self.seat)
            me = v["me"]
            legal = A.legal(g, self.seat)
            written = a
            if snap is not None and isinstance(a, int) and k > 0:
                snap.note_hand(me)
                a = snap.remap(a, me)
            if not isinstance(a, int) or a not in legal:
                self.stats["illegal"] += 1
                what = f"action {written}" + (f" (number {k + 1} of your list)" if k else "")
                return done, (f"{what} is not legal at that point; choose from the listed "
                              "actions")
            if a == A.END and self.end_guard:
                if self._guard_turn != g.turn:
                    self._guard_turn, self._guard_seen = g.turn, set()
                elif self.end_guard == "once":  # runs before "ask again on a new reason"
                    self._guard_seen.add("*")
                why = [] if "*" in self._guard_seen else [
                    (key, text) for key, text in end_check(g, self.seat, v)
                    if key not in self._guard_seen]
                if why:
                    self._guard_seen |= {key for key, _ in why}
                    return done, ("you tried to end the turn, but " + "; and ".join(
                        t for _, t in why) + ". Use the listed actions, or send 0 again to "
                        "end the turn anyway")
            hand0, shop0 = _ids(me)
            desc = A.describe(a, v)
            g.step(self.seat, a)
            self.stats["actions"] += 1
            done += 1
            turn_log.append(desc)
            if a == A.END or not g.needs_action(self.seat):
                return done, ""
            me1 = V.public_view(g, self.seat)["me"]
            hand1, shop1 = _ids(me1)
            new_hand = len(hand1 - hand0) - (1 if A.BUY <= a < A.SELL else 0)
            if me1.get("pending"):
                return done, "a choice is pending"
            if a in (A.REFRESH, A.HERO_POWER, A.HERO_POWER + 1, A.DARK_GIFT) \
                    or shop1 - shop0 or new_hand > 0:
                return done, "new cards were revealed"
        return done, ""

    def _keep_combats(self, g):
        """Store the agent's combats of the last round (with frames) not stored yet."""
        for c in json.loads(g.public_view(self.seat, True)).get("combats", []):
            key = (c["turn"], c["a"], c["b"])
            if self.seat in (c["a"], c["b"]) and key not in {
                    (x["turn"], x["a"], x["b"]) for x in self.combats}:
                self.combats.append(c)

    def play(self) -> dict:
        g = self.game = _core.Game(self.seed, self.config_json)
        g.set_record_log(True)  # combat events for the agent's view (does not change the RNG)
        notes = [""]
        turn, turn_log, turn_actions, last_turn, seen = -1, [], 0, 0, 0
        t0 = time.time()
        for _ in range(200000):
            if self.record_combats and g.turn != seen:  # a round was fought
                self._keep_combats(g)
            seen = g.turn
            if g.is_over():
                break
            for p in range(g.num_players):
                if p != self.seat and g.needs_action(p):
                    g.step(p, g.bot_action(p))
            if not g.needs_action(self.seat):
                continue
            last_turn = g.turn
            if g.turn != turn:
                turn, turn_log = g.turn, []
                turn_actions = self.stats["actions"]
            if self.stats["actions"] - turn_actions >= self.turn_cap:
                self.stats["forced_ends"] += 1
                legal = A.legal(g, self.seat)
                g.step(self.seat, A.END if A.END in legal else self._fallback(g))
                continue
            self._agent_turn(g, turn_log, notes)
        if self.record_combats:
            self._keep_combats(g)
        place = list(g.placements())[self.seat]
        return {"seed": self.seed, "seat": self.seat, "placement": int(place),
                "turns": last_turn, "game_turns": g.turn, "health": list(g.health())[self.seat],
                "seconds": round(time.time() - t0, 1), **self.stats,
                "settings": {"turn_cap": self.turn_cap, "retries": self.retries,
                             "max_plan": self.max_plan, "end_guard": self.end_guard,
                             "plans": self.plans,
                             "config": json.loads(self.config_json or "null")}}
