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


def _late_game(turn=9, seed=9):
    g = taverngym.Game(seed=seed)
    g.set_record_log(True)
    while not (g.turn >= turn and g.needs_action(0)):
        for p in range(8):
            if g.needs_action(p):
                g.step(p, g.bot_action(p))
    while V.public_view(g, 0)["me"].get("pending"):  # a start-of-turn offer: take one
        g.step(0, g.bot_action(0))
    return g


def test_lobby_state_with_an_activate_target_choice(tmp_path):
    """Sky-hatch Runaway's Activate opens a target choice whose `options` the engine view
    sets to null once cards have live texts: the page's state must still be served."""
    from tavernbench import engine as E
    from tavernbench.play.serve import Lobby
    lobby = Lobby(9, [0], [], None, log_dir=tmp_path)
    lobby.g = _late_game()
    for c in ("BG36_242", "BG36_243"):  # Bronze Timewalker (Rally), Sky-hatch Runaway
        E.dbg(lobby.g, cmd="clear_zone", p=0, zone="board") if c == "BG36_242" else None
        E.add(lobby.g, 0, "board", c)
    E.dbg(lobby.g, cmd="set_player", p=0, gold=5)
    lobby.g.step(0, A.ACTIVATE + 1)

    class LiveTexts:
        """The game, with the view it gives once cards have live texts: the engine then
        writes `options: null` into a target choice."""

        def __init__(self, g):
            self._g = g

        def __getattr__(self, k):
            return getattr(self._g, k)

        def public_view(self, p, combats=True):
            v = json.loads(self._g.public_view(p, combats))
            if v["me"].get("pending"):
                v["me"]["pending"]["options"] = None
            return json.dumps(v)

    lobby.g = LiveTexts(lobby.g)
    st = lobby.state(0)
    assert st["view"]["me"]["pending"]["kind"] == "target"
    assert A.CHOOSE in [x["a"] for x in st["legal"]]
    assert "Bronze Timewalker" in V.render(V.public_view(lobby.g, 0))
    assert "Bronze Timewalker" in st["view"]["me"]["pending"]["options"][0]


def test_dark_gift_effects_are_shown():
    g = _late_game()
    v = V.public_view(g, 0)
    if not (v["me"].get("dark_gift") or {}).get("available"):
        return
    g.step(0, A.DARK_GIFT)
    v = V.public_view(g, 0)
    assert v["me"]["pending"]["kind"] == "discover"
    opts = V.pending_options(v["me"])
    assert opts and all("[Dark Gift " in o for o in opts)


def test_referenced_cards_and_terms():
    from tavernbench import engine as E
    g = _late_game()
    E.add(g, 0, "hand", "BG36_242")  # Bronze Timewalker: "Rally: Get a random Chromadrake."
    v = V.public_view(g, 0)
    text = V.render(v)
    assert "Chromadrake (one of):" in text and "Blue Chromadrake" in text
    assert "- Rally: triggers whenever this minion attacks." in text
    assert "Cards in this lobby's Tavern" not in text
    assert "Cards in this lobby's Tavern" in V.render(v, pool=True)


def test_lobby_save_and_resume(tmp_path):
    from tavernbench.play.serve import Lobby
    a = Lobby(9, [0], [], None, log_dir=tmp_path)
    a.g = _late_game(turn=4)
    a.record({"seat": 0, "kind": "human", "turn": 4, "did": ["x"]})
    a.save()
    b = Lobby(1, [0], [], None, log_dir=tmp_path)
    b.resume(tmp_path)
    assert b.g.turn == a.g.turn and len(b.history) == 1
    assert json.loads(b.g.state_json())["players"][0]["gold"] == \
        json.loads(a.g.state_json())["players"][0]["gold"]


def test_human_page_text_is_what_the_model_gets(tmp_path):
    """The lobby page shows the human the same text the driver gives a model for that seat."""
    from tavernbench.play.serve import Lobby
    lobby = Lobby(9, [0], [], None, log_dir=tmp_path)
    lobby.g = _late_game()
    d = Match(AG.BotAgent(), seed=9)._decision(lobby.g, [], "", "")
    assert lobby.state(0)["state_text"] == V.render(
        V.public_view(lobby.g, 0), V.own_combats(lobby.g, 0, detail=True))
    # the driver's first decision of a turn adds only the lobby's card pool to that text
    assert d.state_text.startswith(lobby.state(0)["state_text"].split("\nCards mentioned")[0])


def test_hand_and_lobby_details_are_in_the_text():
    g = _late_game()
    v = V.public_view(g, 0)
    me = v["me"]
    assert len(me["hand"]) >= 2
    me["hand"][0]["locked"] = 2
    me["hand"][0]["link"], me["hand"][1]["link"] = me["hand"][1]["hid"], me["hand"][0]["hid"]
    me["gold_next_turn"] = 1
    text = V.render(v)
    assert "(locked: playable in 2 turns)" in text
    assert "(hand 0 and hand 1 are linked: playing one discards the other)" in text
    assert "You get 1 extra Gold next turn." in text
    assert any(o.get("last_damage") for o in v["opponents"]) and "last combat: " in text
