"""Play TavernGym in the browser: human seats, language-model seats and bots in one lobby.

    python -m tavernbench.play.serve --humans 0 --llm 1,2 --backend claude-cli --model sonnet
    python -m tavernbench.play.serve --humans 0,1 --llm 2 --model MODEL --base-url URL \
        --key-env API_KEY [--llm-agent tools] [--seed 7] [--port 8765] [--host 0.0.0.0]

Seats not listed play as the scripted bot. Open http://HOST:PORT/ (with several human seats,
pick yours there; each seat has its own page, /?seat=k). Recruit turns are simultaneous as in
the game: bots end their turns at once, language models think in the background on copies of
the game, and combat starts when every living player has ended the turn. A page sees only its
own seat's view (`Game.public_view`); seats are not password-protected, so on a shared network
everyone is trusted not to open another player's page.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from taverngym import _core

from ..run_openai import DEFAULT_ENV
from . import actions as A
from . import agents as AG
from . import view as V
from .driver import Match
from .replay import slim


def _ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


class LLMSeat:
    """One language-model seat: its agent, memo, this turn's log and failure count. The
    decision is taken without the lobby lock, on a copy of the game."""

    def __init__(self, lobby, seat, agent, label, turn_cap, retries, max_plan):
        self.lobby, self.seat, self.agent, self.label = lobby, seat, agent, label
        self.m = Match(agent, lobby.seed, seat, turn_cap=turn_cap, retries=retries,
                       max_plan=max_plan)
        self.turn, self.turn_log, self.turn_actions, self.notes = -1, [], 0, [""]
        self.fails, self.busy, self.thought = 0, False, ""

    def step(self):
        lb, m, p = self.lobby, self.m, self.seat
        try:
            with lb.lock:
                g = lb.g
                if g.is_over() or not g.needs_action(p):
                    return
                if g.turn != self.turn:
                    self.turn, self.turn_log = g.turn, []
                    self.turn_actions = m.stats["actions"]
                if m.stats["actions"] - self.turn_actions >= m.turn_cap:
                    m.stats["forced_ends"] += 1
                    g.step(p, A.END if A.END in A.legal(g, p) else g.bot_action(p))
                    lb.bump()
                    return
                feedback, m._feedback = m._feedback, ""
                d = m._decision(g, self.turn_log, feedback, self.notes[0])
                d.game = g.copy()  # the agent may read or simulate on it without the lock
            t0 = time.time()
            try:
                plan = self.agent.decide(d)
            except Exception as e:  # noqa: BLE001 - the bot decides after repeated failures
                plan = AG.Plan([], info={"error": f"{type(e).__name__}"})
            if plan.notes is not None:
                self.notes[0] = plan.notes[:2000]
            info = plan.info
            self.thought = info.get("reasoning") or AG.parse_reasoning(info.get("response", ""))
            with lb.lock:
                if not lb.g.needs_action(p):
                    return
                n0 = len(self.turn_log)
                done, fb = m._execute(lb.g, plan.actions[:m.max_plan], self.turn_log)
                lb.record({"seat": p, "kind": "llm", "turn": d.view["turn"],
                           "phase": d.view["phase"], "state": d.state_text,
                           "feedback": d.feedback, "plan": plan.actions,
                           "did": self.turn_log[n0:], "stop": fb, "notes": plan.notes,
                           "latency_s": round(time.time() - t0, 2),
                           **{k: info.get(k) for k in (
                               "prompt", "response", "reasoning", "thinking", "tools",
                               "error", "cost_usd", "input_tokens", "output_tokens")
                              if info.get(k) is not None}})
                if done:
                    self.fails = 0
                    m._feedback = fb
                else:
                    self.fails += 1
                    m._feedback = fb
                    if self.fails > m.retries:
                        self.fails = 0
                        a = lb.g.bot_action(p)
                        desc = A.describe(a, V.public_view(lb.g, p))
                        lb.g.step(p, a)
                        lb.record({"seat": p, "kind": "llm", "turn": lb.g.turn,
                                   "did": [f"(bot, after {m.retries + 1} failed replies) {desc}"]})
                lb.bump()
        finally:
            self.busy = False


class Lobby:
    def __init__(self, seed, humans, llm_seats, make_agent, config=None, turn_cap=40,
                 retries=2, max_plan=10, show_thoughts=False, label="AI", log_dir=None):
        self.seed = seed
        self.history: list[dict] = []
        self.log = None
        if log_dir:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            self.log = open(Path(log_dir) / "history.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        self.g = _core.Game(seed, json.dumps(config) if config else None)
        self.g.set_record_log(True)
        self.lock = threading.RLock()
        self.version = 0
        self.humans = set(humans)
        self.llm = {p: LLMSeat(self, p, make_agent(p), label, turn_cap, retries, max_plan)
                    for p in llm_seats}
        self.show_thoughts = show_thoughts
        self.started = time.time()

    def bump(self):
        self.version += 1

    def record(self, entry: dict):
        """One step of a seat's history: shown in the page and appended to history.jsonl."""
        entry = {"i": len(self.history), "name": self.name(entry["seat"]),
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        self.history.append(entry)
        if self.log:
            self.log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.log.flush()
        self.bump()

    def visible(self, p: int, entry: dict) -> bool:
        """A page sees its own history; other seats' only after the game (or, for model
        seats, live with --show-thoughts)."""
        return entry["seat"] == p or self.g.is_over() or (
            self.show_thoughts and entry["kind"] == "llm")

    def history_for(self, p: int, after: int) -> dict:
        with self.lock:
            items = [e for e in self.history[after + 1:] if self.visible(p, e)]
            return {"items": items, "last": len(self.history) - 1,
                    "hidden": not self.show_thoughts and not self.g.is_over()}

    def kind(self, p):
        return "human" if p in self.humans else ("llm" if p in self.llm else "bot")

    def name(self, p):
        k = self.kind(p)
        if k == "human":
            return f"Human {sorted(self.humans).index(p) + 1}" if len(self.humans) > 1 else "You"
        return self.llm[p].label if k == "llm" else "Bot"

    def run(self):
        """Step the bots and start language-model decisions until the game ends."""
        while True:
            with self.lock:
                if self.g.is_over():
                    self.bump()
                    return
                moved = True
                while moved:
                    moved = False
                    for p in range(self.g.num_players):
                        if self.kind(p) == "bot" and self.g.needs_action(p):
                            self.g.step(p, self.g.bot_action(p))
                            moved = True
                            self.bump()
                for p, s in self.llm.items():
                    if not s.busy and self.g.needs_action(p):
                        s.busy = True
                        threading.Thread(target=s.step, daemon=True).start()
            time.sleep(0.05)

    # ------------------------------------------------------------------ the page's API

    def state(self, p: int) -> dict:
        with self.lock:
            g = self.g
            full = json.loads(g.public_view(p, True))
            me = full["me"]
            legal = A.legal(g, p) if not g.is_over() and g.needs_action(p) else []
            v = {k: full[k] for k in ("turn", "phase", "alive", "lobby_races")}
            v["me"] = me
            hp = [{"slot": h.get("slot", 0), "name": h.get("name"), "text": V._text(h),
                   "price": h.get("price"), "usable": h.get("usable"),
                   "passive": h.get("passive")} for h in me.get("hero_powers") or []]
            pend = me.get("pending")
            pending = None
            if pend:
                opts = V.pending_options(me)
                cards = [slim(o) if isinstance(o, dict) and pend.get("kind") in (
                    "discover", "timewarp") else None for o in pend.get("options", [])]
                pending = {"kind": pend.get("kind"), "options": opts, "cards": cards,
                           "source": (pend.get("source") or {}).get("name"),
                           "targets": pend.get("targets") or []}
            opps = []
            for o in full["opponents"]:
                lw = o.get("last_warband") or {}
                opps.append({
                    "id": o["id"], "name": self.name(o["id"]), "kind": self.kind(o["id"]),
                    "hero": (o.get("hero") or {}).get("name"), "health": o["health"],
                    "armor": o["armor"], "tier": o["tier"], "alive": o["alive"],
                    "placement": o.get("placement"), "streak": o.get("win_streak"),
                    "next": o.get("is_next_opponent"), "last": o.get("was_last_opponent"),
                    "tribes": [f"{c['count']} {c['race'].title()}" for c in lw.get("counts", [])],
                    "seen": [slim(c) for c in o.get("seen_board") or []],
                    "seen_turn": o.get("seen_turn"),
                    "status": self._status(o["id"]),
                    "thought": self.llm[o["id"]].thought if self.show_thoughts and
                    o["id"] in self.llm else ""})
            combats = []
            for c in full.get("combats", []):
                if p not in (c["a"], c["b"]):
                    continue
                side = 0 if c["a"] == p else 1
                other = c["b"] if side == 0 else c["a"]
                combats.append({
                    "turn": c["turn"], "opponent": other,
                    "opponent_name": None if other is None else self.name(other),
                    "result": "tie" if c["winner"] == 2 else (
                        "win" if c["winner"] == side else "loss"), "damage": c["damage"],
                    "frames": [{"log": f["log"], "attacks": f["attacks"],
                                "me": [slim(x) for x in f["boards"][side]],
                                "opp": [slim(x) for x in f["boards"][1 - side]]}
                               for f in c.get("frames", [])]})
            return {
                "version": self.version, "seat": p, "name": self.name(p), "view": {
                    **v, "me": {
                        "hero": (me.get("hero") or {}).get("name"),
                        "health": me["health"], "armor": me["armor"], "tier": me["tier"],
                        "gold": me["gold"], "max_gold": me["max_gold"], "alive": me["alive"],
                        "placement": me.get("placement"), "frozen": me.get("frozen"),
                        "upgrade_price": me.get("upgrade_price"),
                        "refresh_price": me.get("refresh_price"),
                        "can_upgrade": me.get("can_upgrade_tier"),
                        "hand_limit": me.get("hand_limit", 10), "hero_powers": hp,
                        "trinkets": [slim(t) for t in me.get("trinkets") or []],
                        "quests": [{"name": q["quest"].get("name"), "text": V._text(q["quest"]),
                                    "progress": q.get("progress"), "goal": q.get("goal")}
                                   for q in me.get("quests") or []],
                        "board": [slim(c) for c in me["board"]],
                        "hand": [slim(c) for c in me["hand"]],
                        "shop": [slim(c) for c in me["shop"]],
                        "pending": pending, "cancellable": me.get("cancellable"),
                        "dark_gift": (me.get("dark_gift") or {}).get("available")}},
                "legal": [{"a": a, "label": A.describe(a, full)} for a in legal],
                "lobby": opps, "combats": combats, "over": g.is_over(),
                "history_last": len(self.history) - 1,
                "placements": list(g.placements()) if g.is_over() else None,
                "names": {q: self.name(q) for q in range(g.num_players)},
                "waiting": [self.name(q) + f" (player {q})" for q in range(g.num_players)
                            if q != p and self._status(q) in ("thinking", "playing")],
            }

    def _status(self, q):
        g = self.g
        if g.is_over():
            return "done"
        if not g.needs_action(q):
            return "ready"
        if q in self.llm:
            return "thinking"
        return "playing"

    def act(self, p: int, a: int) -> str | None:
        with self.lock:
            if p not in self.humans:
                return "this seat is not played by a person"
            if self.g.is_over() or not self.g.needs_action(p):
                return "it is not your turn to act"
            if a not in A.legal(self.g, p):
                return "that action is not legal now"
            v = V.public_view(self.g, p)
            desc = A.describe(a, v)
            self.g.step(p, a)
            self.record({"seat": p, "kind": "human", "turn": v["turn"], "phase": v["phase"],
                         "plan": [a], "did": [desc]})
        return None


def handler(lobby: Lobby, page: str, chooser: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _seat(self, q):
            try:
                p = int((q.get("seat") or [""])[0])
            except ValueError:
                p = min(lobby.humans) if len(lobby.humans) == 1 else -1
            return p if p in lobby.humans else None

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                if "seat" not in q and len(lobby.humans) > 1:
                    return self._send(200, chooser, "text/html")
                return self._send(200, page, "text/html")
            if u.path == "/api/history":
                p = self._seat(q)
                if p is None:
                    return self._send(404, json.dumps({"error": "no such human seat"}))
                after = int((q.get("after") or ["-1"])[0])
                return self._send(200, json.dumps(lobby.history_for(p, after),
                                                  ensure_ascii=False))
            if u.path == "/api/state":
                p = self._seat(q)
                if p is None:
                    return self._send(404, json.dumps({"error": "no such human seat"}))
                since = (q.get("since") or [""])[0]
                if since and since.isdigit() and int(since) == lobby.version:
                    return self._send(200, json.dumps({"version": lobby.version,
                                                       "same": True}))
                return self._send(200, json.dumps(lobby.state(p)))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/act":
                return self._send(404, json.dumps({"error": "not found"}))
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                p, a = int(body["seat"]), int(body["action"])
            except (ValueError, KeyError, json.JSONDecodeError):
                return self._send(400, json.dumps({"error": "send {seat, action}"}))
            err = lobby.act(p, a)
            if err:
                return self._send(409, json.dumps({"error": err}))
            return self._send(200, json.dumps(lobby.state(p)))

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--humans", default="0", help="seats played in the browser, e.g. 0,1")
    ap.add_argument("--llm", default="", help="seats played by the language model, e.g. 1,2")
    ap.add_argument("--llm-agent", choices=["llm", "tools"], default="llm")
    ap.add_argument("--backend", choices=["openai", "claude-cli"], default="openai")
    ap.add_argument("--model", default="")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--seed", type=int, default=None, help="default: random")
    ap.add_argument("--config", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--show-thoughts", action="store_true",
                    help="show each model seat's latest reasoning in the lobby (it can "
                         "reveal what that player holds)")
    ap.add_argument("--log-dir", default="",
                    help="where history.jsonl goes (default runs/lobby/<seed>-<time>)")
    ap.add_argument("--turn-cap", type=int, default=40)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--max-plan", type=int, default=10)
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--sims", type=int, default=256)
    ap.add_argument("--env", default=DEFAULT_ENV)
    ap.add_argument("--key-env", default="OPENAI_API_KEY")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--extra", default="")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--read-timeout", type=float, default=300)
    a = ap.parse_args()
    humans, llm = _ints(a.humans), _ints(a.llm)
    if set(humans) & set(llm) or not all(0 <= p < 8 for p in humans + llm):
        ap.error("--humans and --llm must be different seats between 0 and 7")
    if llm and not a.model:
        ap.error("--model is required for --llm seats")
    if a.backend == "claude-cli" and a.llm_agent == "tools":
        ap.error("the claude-cli backend supports --llm-agent llm only")
    seed = a.seed if a.seed is not None else int(time.time()) % 100000

    def make_agent(p):
        client = AG.ClaudeCLIClient(a) if a.backend == "claude-cli" else AG.Client(a)
        return AG.ToolAgent(client, a.calls, a.sims) if a.llm_agent == "tools" else \
            AG.LLMAgent(client)

    label = (a.model or "AI").replace("-", " ").title() + " (AI)"
    log_dir = Path(a.log_dir or f"runs/lobby/{seed}-{time.strftime('%Y%m%d-%H%M%S')}")
    lobby = Lobby(seed, humans, llm, make_agent, json.loads(a.config) if a.config else None,
                  a.turn_cap, a.retries, a.max_plan, a.show_thoughts, label, log_dir)
    system = AG.SYSTEM + (AG.TOOL_NOTE.format(calls=a.calls, sims=a.sims)
                          if a.llm_agent == "tools" else "")
    with open(log_dir / "run.json", "w") as f:
        json.dump({"seed": seed, "humans": humans, "llm": llm, "model": a.model,
                   "backend": a.backend, "agent": a.llm_agent,
                   "system_prompt": system if llm else None,
                   "time": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1)
    page = Path(__file__).with_name("play_ui.html").read_text(encoding="utf-8")
    links = "".join(f'<li><a href="/?seat={p}">Seat {p}</a></li>' for p in sorted(humans))
    chooser = (f"<!doctype html><meta charset=utf-8><title>Choose your seat</title>"
               f"<body style='font:16px system-ui;padding:24px'><h1>Choose your seat</h1>"
               f"<ul>{links}</ul></body>")
    threading.Thread(target=lobby.run, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), handler(lobby, page, chooser))
    host = "localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host
    print(f"seed {seed}. Humans: {humans or 'none'}; {label}: {llm or 'none'}; bots: the rest.")
    print(f"history: {log_dir / 'history.jsonl'}")
    for p in humans:
        print(f"  seat {p}: http://{host}:{a.port}/?seat={p}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
