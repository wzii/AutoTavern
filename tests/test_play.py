"""Full games with a language-model seat (`tavernbench.play`), driven here without a model:
the visible state hides other players' boards, the driver survives bad plans, and the
simulate tool fights only the warband the player has seen."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tavernbench"))

import taverngym  # noqa: E402
from tavernbench.play import actions as A  # noqa: E402
from tavernbench.play import agents as AG  # noqa: E402
from tavernbench.play import view as V  # noqa: E402
from tavernbench.play.driver import Match, Plan  # noqa: E402


def _recruit_game(seed=3, turn=6):
    g = taverngym.Game(seed=seed)
    while not g.is_over() and not (g.turn >= turn and g.needs_action(0)):
        for p in range(8):
            if g.needs_action(p):
                g.step(p, g.bot_action(p))
    return g


def test_public_view_hides_other_boards():
    g = _recruit_game()
    v = V.public_view(g, 0)
    assert "mask" not in v and "config" not in v
    for o in v["opponents"]:
        assert o["board"] == [] and o["last_board"] == []
    full = json.loads(g.view(0))
    assert any(o["board"] for o in full["opponents"])  # what public_view must leave out
    text = V.render(v)
    assert "Your warband" in text and "Tavern:" in text


def test_every_legal_action_is_described():
    g = _recruit_game()
    v = V.public_view(g, 0)
    legal, text = A.listing(g, 0, v)
    assert legal
    assert (A.END in legal) == (f"[{A.END}] end the turn" in text)
    for a in legal:
        assert not A.describe(a, v).startswith("action ")


def test_parse_plan():
    assert AG.parse_plan('x {"actions": [8, "46", 0], "notes": "go"}') == ([8, 46, 0], "go")
    assert AG.parse_plan('```json\n{"action": 3}\n```') == ([3], None)
    assert AG.parse_plan("no json here") == ([], None)


def test_bot_agent_plays_a_full_game():
    row = Match(AG.BotAgent(), seed=5).play()
    assert 1 <= row["placement"] <= 8
    assert row["illegal"] == 0 and row["fallbacks"] == 0


class _Bad:
    """An illegal index every time, then a turn that never ends."""

    def decide(self, d):
        return Plan([999])


class _Stall:
    def decide(self, d):
        return Plan([next((a for a in d.legal if a != A.END), d.legal[0])])


def test_driver_survives_bad_agents():
    row = Match(_Bad(), seed=1, retries=1).play()
    assert row["placement"] >= 1 and row["fallbacks"] > 0 and row["illegal"] > 0
    row = Match(_Stall(), seed=1, turn_cap=15).play()
    assert row["placement"] >= 1 and row["forced_ends"] > 0


def test_simulate_uses_the_seen_warband_only():
    g = _recruit_game(seed=3, turn=6)
    me = V.public_view(g, 0)["me"]
    r = AG.simulate(g, 0, n=64)
    if "error" in r:  # this seed's next opponent was not met yet: nothing to check
        return
    assert r["opponent"] == me["next_opponent"]
    assert abs(r["win"] + r["tie"] + r["loss"] - 1) < 1e-6
    # the live game is untouched
    assert json.loads(g.view(0))["opponents"] == json.loads(_recruit_game(3, 6).view(0))[
        "opponents"]


class _BuyThenEnd:
    """Buys the first card it can and ends the turn in the same plan."""

    def __init__(self):
        self.feedback = []

    def decide(self, d):
        if d.feedback:
            self.feedback.append(d.feedback)
        buys = [a for a in d.legal if A.BUY <= a < A.SELL]
        if buys and d.view["phase"] == "recruit" and not d.view["me"]["board"]:
            return Plan([buys[0], A.END])
        return Plan([A.END] if A.END in d.legal else [d.legal[0]])


def test_end_guard_asks_before_ending_with_minions_in_hand():
    agent, recs = _BuyThenEnd(), []
    Match(agent, seed=2, trace=recs.append).play()
    held = [r for r in recs if r["stop"].startswith("you tried to end the turn")]
    assert held and any(f.startswith("you tried to end the turn") for f in agent.feedback)
    # the second End Turn of that turn goes through
    t = held[0]["turn"]
    assert any(r["turn"] == t and r["plan"] == [A.END] and r["executed"] == 1 for r in recs)
