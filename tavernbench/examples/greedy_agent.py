"""A minimal agent for tavernbench.play: shows the interface, not a strategy.

    python -m tavernbench.play --agent custom --agent-class examples.greedy_agent:GreedyAgent \
        --seeds 0-9 --out runs/greedy

Each turn: buy the strongest affordable minion and play it in the same plan, upgrade when it
is cheap, then end the turn. Choices take the first option. It fills `info` so traces,
replays and the lobby's history show its reasoning.
"""

from tavernbench.play import actions as A
from tavernbench.play.driver import Decision, Plan


class GreedyAgent:
    def __init__(self, upgrade_at: int = 3):
        self.upgrade_at = upgrade_at  # upgrade when it costs at most this

    def decide(self, d: Decision) -> Plan:
        me = d.view["me"]
        if me.get("pending"):                         # hero, Discover, target, trinket...
            choose = [a for a in d.legal if A.CHOOSE <= a < A.PLAY]
            return Plan([choose[0] if choose else d.legal[0]],
                        info={"reasoning": "take the first option"})
        plan, why = [], []
        # play minions already in hand (rightmost position)
        for h, c in enumerate(me["hand"]):
            a = A.PLAY + h * A.N_POS + len(me["board"])
            if c.get("type") == "minion" and a in d.legal:
                plan.append(a)
                why.append(f"play {c['name']}")
                break
        if not plan and A.UPGRADE in d.legal and (me.get("upgrade_price") or 99) <= self.upgrade_at:
            plan.append(A.UPGRADE)
            why.append("upgrade while it is cheap")
        if not plan:
            buys = [(c["atk"] + c["hp"], i) for i, c in enumerate(me["shop"])
                    if c.get("type") == "minion" and A.BUY + i in d.legal]
            if buys and len(me["board"]) < 7:
                _, i = max(buys)
                # the bought card joins the hand at len(hand): play it in the same plan
                plan += [A.BUY + i, A.PLAY + len(me["hand"]) * A.N_POS + len(me["board"])]
                why.append(f"buy and play {me['shop'][i]['name']}")
        plan.append(A.END)
        return Plan(plan, info={"reasoning": "; ".join(why) or "nothing to do"})
