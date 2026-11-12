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


def test_warband_and_seen_minions_render_with_stats():
    g = _recruit_game(seed=3, turn=6)
    v = V.public_view(g, 0)
    text = V.render(v)
    for m in v["me"]["board"]:
        assert f"{m['name']} (" in text and f"{m['atk']}/{m['hp']}" in text
    seen = [m for o in v["opponents"] for m in o.get("seen_board") or []]
    assert v["me"]["board"] and seen
    for c in v["me"]["board"] + seen:
        assert V.is_minion(c)
        assert "(spell" not in V.card(c)


def _game_with(cond, seed=3, max_turn=12):
    """A copy of a bot game at the first decision of seat 0 that satisfies `cond(view)`."""
    g = taverngym.Game(seed=seed)
    while not g.is_over() and g.turn <= max_turn:
        if g.needs_action(0) and cond(V.public_view(g, 0)):
            return g
        for p in range(8):
            if g.needs_action(p):
                g.step(p, g.bot_action(p))
    return None


def test_plan_indices_follow_cards():
    # Play a hand minion at the far left, then sell "warband 0" as listed before the play:
    # the minion that was leftmost is sold, not the one just played.
    def ok(v):
        me = v["me"]
        return v["phase"] == "recruit" and not me.get("pending") and 1 <= len(me["board"]) < 7 \
            and any(c.get("type") == "minion" for c in me["hand"])
    g = _game_with(ok)
    assert g is not None
    me = V.public_view(g, 0)["me"]
    h = next(i for i, c in enumerate(me["hand"]) if c.get("type") == "minion")
    first = me["board"][0]["eid"]
    played = me["hand"][h]["eid"]
    m = Match(AG.BotAgent(), seed=0)
    done, stop = m._execute(g, [A.PLAY + h * A.N_POS, A.SELL], [])
    after = [c["eid"] for c in V.public_view(g, 0)["me"]["board"]]
    assert done == 2 and first not in after and played in after, stop


def test_buy_then_play_in_one_plan():
    def ok(v):
        me = v["me"]
        return v["phase"] == "recruit" and not me.get("pending") and me["gold"] >= 3 \
            and len(me["board"]) < 7 and len(me["hand"]) < 9 \
            and any(c.get("type") == "minion" for c in me["shop"])
    g = _game_with(ok)
    assert g is not None
    v = V.public_view(g, 0)
    me = v["me"]
    i = next(i for i, c in enumerate(me["shop"]) if c.get("type") == "minion")
    n, b = len(me["hand"]), len(me["board"])
    _, text = A.listing(g, 0, v)
    assert f"[{A.PLAY + n * A.N_POS + b}] play the first card you buy into warband position {b}" in text
    m = Match(AG.BotAgent(), seed=0)
    done, stop = m._execute(g, [A.BUY + i, A.PLAY + n * A.N_POS + b], [])
    assert done == 2 and len(V.public_view(g, 0)["me"]["board"]) == b + 1, stop


def test_prompt_shows_what_a_player_sees():
    """Every opponent's last-seen warband, Hero Power and trinkets, and the last combat as the
    player watched it (both warbands and the events) are in the text."""
    g = taverngym.Game(seed=9)
    g.set_record_log(True)
    while not (g.turn >= 9 and g.needs_action(0)):
        for p in range(8):
            if g.needs_action(p):
                g.step(p, g.bot_action(p))
    v = V.public_view(g, 0)
    text = V.render(v, V.own_combats(g, 0, detail=True))
    for o in v["opponents"]:
        if o["alive"] and o.get("seen_board"):
            assert f"warband you saw on turn {o['seen_turn']}" in text
            assert all(c["name"] in text for c in o["seen_board"])
        for t in o.get("trinkets") or []:
            assert t["name"] in text
    assert "Hero Power" in text and "What happened:" in text
    assert "Their warband at the start:" in text and "Your warband at the start:" in text
    if v["me"].get("deity"):
        assert "Your Deity:" in text
