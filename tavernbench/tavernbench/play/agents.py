"""Agents for `driver.Match`: the scripted bot, random play, a language model answering from the
state alone (`LLMAgent`) and one that can try plays in the engine first (`ToolAgent`)."""

from __future__ import annotations

import json
import random
import re
import time

from taverngym import _core

from .. import render as R
from . import actions as A
from . import view as V
from .driver import Decision, Plan

SYSTEM = (
    "You are playing Hearthstone Battlegrounds (patch 37.0.2, solo mode, eight players) as "
    "one of the players. Your goal is the best final placement (1st is best; top 4 is good). "
    "Each turn you recruit (buy, play, sell, arrange minions, upgrade the Tavern), then your "
    "warband fights another player's automatically.\n\n" + R.RULES_PRIMER + """
- Gold is refilled every turn (3 on turn 1, +1 per turn up to 10); unspent Gold is lost.
- A bought card goes to your hand. Only minions in your warband fight: play a bought minion
  (play hand k into a warband position) before ending the turn, or it does nothing.

## How to answer
At each decision you get the state and the numbered legal actions. You have no tools here
unless they are listed below: do not write tool calls. Reply with one JSON object:
{"reasoning": "<short>", "actions": [<index>, ...], "notes": "<optional memo for later>"}
- "actions" holds one or more indices, executed in order. Only the first is checked against
  the current list; each later index is read in the state after the earlier ones, so account
  for shifts: a bought card goes to the right end of your hand and the Tavern cards to its
  right move left; playing or selling shifts the cards to the right of it left.
- Your plan stops and you are asked again after anything that reveals new cards (refresh,
  Discover, a pending choice, a generated card), or at an illegal index.
- Finish the turn with action 0 (end turn). Minion order matters in combat: leftmost
  attacks first.
- "notes" (optional) replaces your memo, which is shown to you at every later decision: keep
  your game plan there (tribe, key cards, when to upgrade).
"""
)

TOOL_NOTE = """
## Tools
Before answering you may call (at most {calls} calls for this decision):
- inspect(actions): the state after applying the action indices to a copy of the game
  (same reading of indices as your answer), with the new legal actions.
- simulate(actions): the same, then the next combat against the warband you last saw your
  next opponent play, fought {sims} times: win / tie / loss chances and expected damage.
Copies use fresh random numbers: a refresh or Discover in a tool shows a possible outcome, not
the real one. When done, answer with the JSON object only.
"""


def parse_plan(text: str) -> tuple[list[int], str | None]:
    """The action list (and notes) of a reply; [] if none can be read."""
    text = text or ""
    for m in reversed(list(re.finditer(r"\{.*\}", text, re.DOTALL))):
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        acts = d.get("actions", d.get("action"))
        if isinstance(acts, (int, str)):
            acts = [acts]
        if isinstance(acts, list):
            out = []
            for a in acts:
                try:
                    out.append(int(a))
                except (TypeError, ValueError):
                    break
            notes = d.get("notes")
            return out, notes if isinstance(notes, str) and notes.strip() else None
    m = re.search(r'"actions?"\s*:\s*\[?\s*([\d,\s]+)', text)
    if m:
        return [int(x) for x in re.findall(r"\d+", m.group(1))], None
    return [], None


def user_prompt(d: Decision) -> str:
    parts = []
    if d.notes:
        parts.append("## Your notes\n" + d.notes)
    parts.append("## State\n" + d.state_text)
    if d.turn_log:
        parts.append("## Done this turn\n" + "\n".join(f"- {s}" for s in d.turn_log))
    if d.feedback:
        parts.append("## Note\nYour previous plan stopped: " + d.feedback + ".")
    parts.append("## Legal actions\n" + d.actions_text)
    parts.append("Answer with the JSON object.")
    return "\n\n".join(parts)


class BotAgent:
    """The engine's scripted bot (a baseline; its average placement against itself is 4.5)."""

    def decide(self, d: Decision) -> Plan:
        return Plan([d.game.bot_action(d.seat)])


class RandomAgent:
    """Uniformly random legal actions, ending the turn with probability `p_end`."""

    def __init__(self, seed=0, p_end=0.15):
        self.rng, self.p_end = random.Random(seed), p_end

    def decide(self, d: Decision) -> Plan:
        if A.END in d.legal and self.rng.random() < self.p_end:
            return Plan([A.END])
        return Plan([self.rng.choice(d.legal)])


class Client:
    """An OpenAI-compatible chat endpoint (key and address as in `run_openai`)."""

    def __init__(self, a):
        import httpx
        from openai import OpenAI

        from ..run_openai import api_key_and_base
        key, base = api_key_and_base(a)
        self.client = OpenAI(api_key=key, base_url=base, max_retries=0,
                             timeout=httpx.Timeout(connect=20, read=a.read_timeout, write=30,
                                                   pool=None),
                             http_client=httpx.Client(trust_env=not a.no_proxy))
        self.model, self.max_tokens, self.temperature = a.model, a.max_tokens, a.temperature
        self.extra = json.loads(a.extra) if a.extra else None

    def chat(self, messages, tools=None):
        import openai

        from ..run_openai import safe_error
        kw = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            kw["tools"], kw["tool_choice"] = tools, "auto"
        if self.temperature is not None:
            kw["temperature"] = self.temperature
        if self.extra:
            kw["extra_body"] = self.extra
        for attempt in range(6):
            try:
                r = self.client.chat.completions.create(**kw)
                if not getattr(r, "choices", None):
                    raise RuntimeError("reply without choices")
                return r
            except openai.RateLimitError:
                time.sleep(min(60, 5 * 2 ** attempt))
            except Exception as e:  # noqa: BLE001
                if attempt >= 2:
                    raise RuntimeError(safe_error(e)) from None
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("rate limited")


class ClaudeCLIClient:
    """Claude through the Claude Code CLI (`claude -p`, signed in separately), as in
    `run_claude`: the system prompt replaces Claude Code's, no tools, no project settings,
    run from an empty directory. Function calling is not available (bare play only)."""

    def __init__(self, a):
        import tempfile
        from pathlib import Path
        self.model, self.effort, self.timeout = a.model, a.effort, int(a.read_timeout)
        self.cwd = Path(tempfile.gettempdir()) / "tavernbench-neutral-cwd"
        self.cwd.mkdir(exist_ok=True)

    def chat(self, messages, tools=None):
        import subprocess
        from types import SimpleNamespace as NS
        if tools:
            raise RuntimeError("the claude-cli backend has no function calling")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        # No MCP servers either: the account's connectors would add their tool definitions
        # (100k+ tokens) to every call. The prompt goes in on stdin (no argv length limit).
        cmd = ["claude", "-p", "--model", self.model, "--system-prompt", system,
               "--tools", "", "--effort", self.effort, "--output-format", "json",
               "--setting-sources", "", "--strict-mcp-config", "--mcp-config",
               '{"mcpServers": {}}']
        for attempt in range(3):
            try:
                out = subprocess.run(cmd, check=False, cwd=self.cwd, input=prompt,
                                     capture_output=True, text=True, timeout=self.timeout)
                raw = out.stdout.strip()
                data = json.loads(raw[raw.find("{"):]) if "{" in raw else {}
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                data = {"is_error": True}
            if not data.get("is_error") and data.get("result") is not None:
                break
            time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError("claude -p failed")
        u = data.get("usage") or {}
        tin = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                          "cache_creation_input_tokens"))
        return NS(choices=[NS(message=NS(content=data["result"], tool_calls=None),
                              finish_reason=data.get("stop_reason"))],
                  usage=NS(prompt_tokens=tin, completion_tokens=u.get("output_tokens") or 0),
                  cost_usd=data.get("total_cost_usd") or 0.0)


def _usage(r, info):
    info["cost_usd"] = info.get("cost_usd", 0.0) + (getattr(r, "cost_usd", 0.0) or 0.0)
    if getattr(r, "usage", None):
        info["input_tokens"] = info.get("input_tokens", 0) + (r.usage.prompt_tokens or 0)
        info["output_tokens"] = info.get("output_tokens", 0) + (r.usage.completion_tokens or 0)


class LLMAgent:
    """A bare language model: one call per decision, state and legal actions as text."""

    def __init__(self, client: Client, log_text=True):
        self.client, self.log_text = client, log_text

    def decide(self, d: Decision) -> Plan:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_prompt(d)}]
        info = {}
        r = self.client.chat(msgs)
        _usage(r, info)
        text = r.choices[0].message.content or ""
        acts, notes = parse_plan(text)
        if self.log_text:
            info["response"] = text[-2000:]
        return Plan(acts, notes, info)


# ------------------------------------------------------------------ tool use

TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": {"actions": {
            "type": "array", "items": {"type": "integer"},
            "description": "action indices, read as in the answer"}},
            "required": ["actions"]}}}
    for name, desc in (
        ("inspect", ("Apply action indices to a copy of the game and show the resulting "
                     "state and legal actions.")),
        ("simulate", ("Apply action indices to a copy of the game, then fight the next "
                      "combat against the next opponent's last seen warband many times.")))
]


def _splitmix(x):
    x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return x ^ (x >> 31)


def reseeded_copy(g, salt: int):
    """A copy of the game with new random numbers, so a tool cannot reveal the real future
    (the next refresh, a Discover's options, combat luck)."""
    st = json.loads(g.state_json())
    st["rng"]["s"] = [_splitmix(v ^ _splitmix(salt * 4 + i)) for i, v in enumerate(st["rng"]["s"])]
    st["seed"] = _splitmix(st["seed"] ^ salt) % (1 << 63)
    return _core.Game.from_json(json.dumps(st))


def apply_plan(g, seat, plan) -> str | None:
    for k, a in enumerate(plan):
        if not isinstance(a, int) or a not in A.legal(g, seat):
            return f"action {a} (number {k + 1} of the list) is not legal at that point"
        g.step(seat, a)
        if not g.needs_action(seat):
            return None
    return None


def simulate(g, seat, n=256) -> dict:
    """Win / tie / loss of the seat's warband against the warband it last saw its next
    opponent play (the opponent's current warband is not visible)."""
    v = V.public_view(g, seat)
    me = v["me"]
    opp = me.get("next_opponent")
    if opp is None:
        return {"error": "no next opponent is known"}
    st = json.loads(g.state_json())
    sc = st["players"][seat].get("scouted") or []
    seen = sc[opp].get("board") if opp < len(sc) and sc[opp] else None
    ghost = bool(me.get("next_is_ghost"))
    if not seen and not ghost:
        return {"error": "you have not seen the next opponent's warband yet"}
    seen_turn = sc[opp].get("turn") if seen else None
    if seen:
        st["players"][opp]["board"] = seen
    c = _core.Game.from_json(json.dumps(st))
    r = json.loads(c.debug(json.dumps({"cmd": "combat_preview", "a": seat, "b": opp,
                                       "n": n, "ghost_b": ghost})))
    return {"win": round(r["win_a"], 3), "tie": round(r["tie"], 3), "loss": round(r["win_b"], 3),
            "expected_damage_taken": round(r["avg_damage_by_b"] * r["win_b"], 2),
            "expected_damage_dealt": round(r["avg_damage_by_a"] * r["win_a"], 2),
            "opponent": opp, "opponent_warband_seen_on_turn": seen_turn,
            "current_turn": v["turn"]}


class ToolAgent(LLMAgent):
    """A language model that may inspect and simulate plays on copies of the game first."""

    def __init__(self, client: Client, calls=6, sims=256, log_text=True):
        super().__init__(client, log_text)
        self.calls, self.sims = calls, sims
        self.salt = random.Random().randrange(1 << 30)

    def tool(self, d: Decision, name: str, args: dict) -> dict:
        plan = args.get("actions") or []
        self.salt += 1
        g = reseeded_copy(d.game, self.salt)
        err = apply_plan(g, d.seat, plan)
        if err:
            return {"error": err}
        v = V.public_view(g, d.seat)
        if name == "inspect":
            out = {"state": V.render(v)}
            if g.needs_action(d.seat):
                out["legal_actions"] = A.listing(g, d.seat, v)[1]
            return out
        if name == "simulate":
            if v["me"].get("pending"):
                return {"error": "a choice is pending after these actions; add a choose action"}
            out = simulate(g, d.seat, self.sims)
            out["warband"] = [V.card(c, i) for i, c in enumerate(v["me"]["board"])]
            return out
        return {"error": f"unknown tool {name}"}

    def decide(self, d: Decision) -> Plan:
        if len(d.legal) == 1 or d.view["phase"] == "hero_select":
            return super().decide(d)  # nothing to test
        msgs = [{"role": "system", "content": SYSTEM + TOOL_NOTE.format(calls=self.calls,
                                                                         sims=self.sims)},
                {"role": "user", "content": user_prompt(d)}]
        info, calls, text = {"tools": []}, 0, ""
        for _ in range(self.calls + 2):
            r = self.client.chat(msgs, TOOLS if calls < self.calls else None)
            _usage(r, info)
            msg = r.choices[0].message
            tcs = msg.tool_calls or []
            if not tcs:
                text = msg.content or ""
                break
            msgs.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": t.id, "type": "function",
                 "function": {"name": t.function.name, "arguments": t.function.arguments}}
                for t in tcs]})
            for t in tcs:
                calls += 1
                try:
                    args = json.loads(t.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                res = self.tool(d, t.function.name, args) if calls <= self.calls else {
                    "error": "tool budget exhausted; answer now"}
                info["tools"].append({"tool": t.function.name, "args": args,
                                      "result": {k: v for k, v in res.items()
                                                 if k not in ("state", "legal_actions")}})
                msgs.append({"role": "tool", "tool_call_id": t.id, "content": json.dumps(res)})
            if calls >= self.calls:
                msgs.append({"role": "user", "content": "No more tool calls. Answer now with "
                                                        "the JSON object only."})
        info["tool_calls"] = calls
        acts, notes = parse_plan(text)
        if self.log_text:
            info["response"] = text[-2000:]
        return Plan(acts, notes, info)
