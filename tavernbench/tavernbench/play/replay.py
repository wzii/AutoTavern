"""Rebuild a played game from its trace and write it as a self-contained HTML replay.

    python -m tavernbench.play.replay --run runs/MODEL --seed 0 [--out replay.html]

A game is determined by its seed and the agent's plans: the replay feeds the recorded plans
back through the same driver (no model is called), records the combats frame by frame, and
checks that the result matches the original game. The page shows every turn: the state at
each decision, the model's reasoning, the actions it took, and each combat step by step.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import view as V
from .driver import Match, Plan

KW_SHORT = {"DIVINE_SHIELD": "Divine Shield", "TAUNT": "Taunt", "VENOMOUS": "Venomous",
            "POISONOUS": "Poisonous", "REBORN": "Reborn", "WINDFURY": "Windfury",
            "MEGA_WINDFURY": "Mega-Windfury", "CLEAVE": "Cleave", "STEALTH": "Stealth"}


def slim(c: dict) -> dict:
    """A card as the page draws it."""
    spell = c.get("type") == "spell" or c.get("kind") not in (None, "minion")
    return {"n": c.get("name", "?"), "a": c.get("atk", 0), "h": c.get("hp", 0),
            "t": c.get("tier", 0), "g": bool(c.get("golden")), "s": spell,
            "k": [KW_SHORT.get(k, k.replace("_", " ").title()) for k in c.get("keywords") or []
                  if k != "MAGNETIC"],
            "r": [r.title() for r in c.get("races") or []], "x": V._text(c),
            "e": c.get("eid"), "p": c.get("price"),
            "at": [{"n": a.get("name", "?"), "x": V._text(a), "g": a.get("kind") == "dark_gift"}
                  for a in c.get("attached") or []]}


def _reply(info: dict) -> dict:
    """The reasoning and notes of a recorded reply (or its raw text)."""
    text = info.get("response") or ""
    for m in reversed(list(re.finditer(r"\{.*\}", text, re.DOTALL))):
        try:
            d = json.loads(m.group(0))
            return {"reasoning": d.get("reasoning", ""), "notes": d.get("notes") or ""}
        except json.JSONDecodeError:
            continue
    return {"reasoning": text.strip()[:600], "notes": "", "unparsed": bool(text.strip())}


class ReplayAgent:
    """Returns the recorded plans in order and keeps a snapshot of every decision."""

    def __init__(self, recs: list[dict]):
        self.recs, self.i, self.snaps = recs, 0, []

    def decide(self, d) -> Plan:
        if self.i >= len(self.recs):
            raise RuntimeError("the game asked for more decisions than the trace holds")
        r = self.recs[self.i]
        self.i += 1
        me = d.view["me"]
        self.snaps.append({
            "turn": d.view["turn"], "phase": d.view["phase"], "state": d.state_text,
            "feedback": d.feedback,
            "me": {"hero": (me.get("hero") or {}).get("name"), "health": me["health"],
                   "armor": me["armor"], "tier": me["tier"], "gold": me["gold"],
                   "max_gold": me["max_gold"], "board": [slim(c) for c in me["board"]],
                   "hand": [slim(c) for c in me["hand"]], "shop": [slim(c) for c in me["shop"]],
                   "pending": V.pending_options(me) if me.get("pending") else [],
                   "pending_kind": (me.get("pending") or {}).get("kind")},
            "lobby": [{"id": o["id"], "hero": (o.get("hero") or {}).get("name"),
                       "health": o["health"], "armor": o["armor"], "tier": o["tier"],
                       "alive": o["alive"], "next": o.get("is_next_opponent")}
                      for o in d.view.get("opponents", [])],
        })
        info = r.get("info") or {}
        if not r.get("plan") and "error" in info:
            return Plan([], info={"error": info["error"]})
        return Plan(list(r.get("plan") or []))


def _jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def rebuild(run: Path, seed: int) -> dict:
    rows = _jsonl(run / "games.jsonl")
    row = next((r for r in rows if r["seed"] == seed), None)
    if row is None:
        raise SystemExit(f"seed {seed} is not in {run / 'games.jsonl'}")
    recs = _jsonl(run / "traces" / f"{seed}.jsonl")
    st = row.get("settings") or {}
    agent = ReplayAgent(recs)
    guard = st.get("end_guard", False)
    if guard is True and "plans" not in st:  # recorded before the check asked again
        guard = "once"
    replayed = []
    m = Match(agent, seed, row.get("seat", 0), st.get("config"), st.get("turn_cap", 40),
              st.get("retries", 2), st.get("max_plan", 10), replayed.append,
              record_combats=True, end_guard=guard, plans=st.get("plans", "sequential"))
    out = m.play()
    keys = ("placement", "actions", "illegal", "fallbacks", "health", "turns")
    verified = all(out.get(k) == row.get(k) for k in keys if k in row) \
        and agent.i == len(recs)
    decisions = []
    for snap, orig, rep in zip(agent.snaps, recs, replayed):
        info = orig.get("info") or {}
        reply = _reply(info)
        if info.get("reasoning"):
            reply["reasoning"] = info["reasoning"]
        if orig.get("notes"):
            reply["notes"] = orig["notes"]
        decisions.append({**snap, **reply, "plan": orig.get("plan"),
                          "did": rep.get("did", []), "stop": rep.get("stop", ""),
                          "cost": info.get("cost_usd"), "latency": orig.get("latency_s"),
                          "tools": info.get("tools") or [], "error": info.get("error"),
                          "prompt": info.get("prompt") or "", "response": info.get("response")
                          or "", "thinking": info.get("thinking") or "",
                          "told": orig.get("feedback") or snap.get("feedback") or ""})
    seat = row.get("seat", 0)
    combats = []
    for c in m.combats:
        side = 0 if c["a"] == seat else 1
        other = c["b"] if side == 0 else c["a"]
        res = "tie" if c["winner"] == 2 else ("win" if c["winner"] == side else "loss")
        combats.append({
            "turn": c["turn"], "opponent": other, "result": res, "damage": c["damage"],
            "frames": [{"log": f["log"], "attacks": f["attacks"],
                        "me": [slim(x) for x in f["boards"][side]],
                        "opp": [slim(x) for x in f["boards"][1 - side]]}
                       for f in c.get("frames", [])]})
    heroes = {}
    for o in (agent.snaps[-1]["lobby"] if agent.snaps else []):
        heroes[o["id"]] = o["hero"]
    return {"meta": {**{k: row.get(k) for k in (
        "seed", "seat", "placement", "turns", "decisions", "actions", "illegal", "fallbacks",
        "cost_usd", "input_tokens", "output_tokens", "seconds", "agent", "model")},
        "verified": verified, "replayed": {k: out.get(k) for k in keys}},
        "heroes": heroes, "decisions": decisions, "combats": combats}


def write_html(data: dict, path: Path):
    tpl = (Path(__file__).with_name("replay_template.html")).read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    m = data["meta"]
    title = f"{m.get('model') or m.get('agent')} Seed {m['seed']} Replay".title()
    html = tpl.replace("__TITLE__", title).replace("/*__DATA__*/null", blob)
    path.write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="a run directory of tavernbench.play")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", default="", help="default: <run>/replays/<seed>.html")
    a = ap.parse_args()
    run = Path(a.run)
    data = rebuild(run, a.seed)
    out = Path(a.out) if a.out else run / "replays" / f"{a.seed}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_html(data, out)
    m = data["meta"]
    print(f"{out}: place {m['placement']}, {len(data['decisions'])} decisions, "
          f"{len(data['combats'])} combats, "
          + ("matches the original game" if m["verified"] else
             f"DOES NOT match the original game: replayed {m['replayed']}"))


if __name__ == "__main__":
    main()
