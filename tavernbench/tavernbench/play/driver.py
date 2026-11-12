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


def _ids(me: dict) -> tuple[set, set]:
    return ({c.get("hid") for c in me["hand"]}, {c.get("sid") for c in me["shop"]})


class Match:
    def __init__(self, agent, seed: int, seat: int = 0, config: dict | None = None,
                 turn_cap: int = 40, retries: int = 2, max_plan: int = 10, trace=None):
        self.agent, self.seed, self.seat = agent, seed, seat
        self.config_json = json.dumps(config) if config else None
        self.turn_cap, self.retries, self.max_plan = turn_cap, retries, max_plan
        self.trace = trace  # a callable taking one dict per decision, or None
        self.stats = {"decisions": 0, "actions": 0, "illegal": 0, "fallbacks": 0,
                      "forced_ends": 0, "input_tokens": 0, "output_tokens": 0,
                      "tool_calls": 0, "errors": 0, "cost_usd": 0.0}
        self._feedback = ""  # an illegal action that cut the last plan short

    def _decision(self, g, turn_log, feedback, notes) -> Decision:
        v = V.public_view(g, self.seat)
        legal, text = A.listing(g, self.seat, v)
        combats = V.own_combats(g, self.seat) if v["phase"] == "recruit" and not turn_log \
            else []
        return Decision(g, self.seat, v, V.render(v, combats), legal, text, list(turn_log),
                        feedback, notes)

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
            rec = {"turn": g.turn, "phase": d.view["phase"], "attempt": attempt,
                   "plan": plan.actions, "info": plan.info, "latency_s": round(time.time() - t0,
                                                                              2)}
            done, feedback = self._execute(g, plan.actions[:self.max_plan], turn_log)
            rec["executed"], rec["stop"] = done, feedback
            if self.trace:
                self.trace(rec)
            if done:
                self._feedback = feedback if feedback.startswith("action") else ""
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
        for a in plan:
            if not g.needs_action(self.seat):
                return done, ""
            v = V.public_view(g, self.seat)
            legal = A.legal(g, self.seat)
            if not isinstance(a, int) or a not in legal:
                self.stats["illegal"] += 1
                return done, (f"action {a} is not legal in the current state; choose from the "
                              "listed actions")
            me = v["me"]
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

    def play(self) -> dict:
        g = _core.Game(self.seed, self.config_json)
        notes = [""]
        turn, turn_log, turn_actions, last_turn = -1, [], 0, 0
        t0 = time.time()
        for _ in range(200000):
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
        place = list(g.placements())[self.seat]
        return {"seed": self.seed, "seat": self.seat, "placement": int(place),
                "turns": last_turn, "game_turns": g.turn, "health": list(g.health())[self.seat],
                "seconds": round(time.time() - t0, 1), **self.stats}
