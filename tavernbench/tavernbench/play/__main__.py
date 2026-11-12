"""Play complete games with a language model (or a baseline) in one seat against seven bots.

    python -m tavernbench.play --agent llm   --model MODEL --seeds 0-19 --out runs/MODEL
    python -m tavernbench.play --agent tools --model MODEL --seeds 0-19 --out runs/MODEL-tools \
        [--base-url URL] [--key-env OPENAI_API_KEY | --env FILE] [--extra '{...}'] [--jobs 4]
    python -m tavernbench.play --agent llm --backend claude-cli --model sonnet --seeds 0-2 \
        --out runs/sonnet          (through the signed-in Claude Code CLI; no tools)
    python -m tavernbench.play --agent bot|random --seeds 0-99 --out runs/bot

Writes `games.jsonl` (one row per game: placement, turns, decisions, illegal actions,
fallbacks to the bot, tokens) and `traces/<seed>.jsonl` (every decision: plan, executed
actions, the reply and tool calls). Resumable: seeds already in games.jsonl are skipped. The
key handling follows run_openai.py (never written anywhere).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import threading
from pathlib import Path

from ..run_openai import DEFAULT_ENV
from . import agents as AG
from .driver import Match


def seeds(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def make_agent(a, seed):
    if a.agent == "bot":
        return AG.BotAgent()
    if a.agent == "random":
        return AG.RandomAgent(seed)
    if a.backend == "claude-cli":
        if a.agent == "tools":
            raise SystemExit("--backend claude-cli supports --agent llm only")
        return AG.LLMAgent(AG.ClaudeCLIClient(a))
    client = AG.Client(a)
    if a.agent == "llm":
        return AG.LLMAgent(client)
    return AG.ToolAgent(client, calls=a.calls, sims=a.sims)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["llm", "tools", "bot", "random"], default="llm")
    ap.add_argument("--model", default="")
    ap.add_argument("--backend", choices=["openai", "claude-cli"], default="openai",
                    help="claude-cli: Claude through the signed-in Claude Code CLI")
    ap.add_argument("--effort", default="low", help="claude-cli effort level")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="0-9")
    ap.add_argument("--seat", type=int, default=0)
    ap.add_argument("--config", default="", help="RulesConfig fields as a JSON object")
    ap.add_argument("--turn-cap", type=int, default=40, help="agent actions per turn")
    ap.add_argument("--max-plan", type=int, default=10, help="actions executed per reply")
    ap.add_argument("--retries", type=int, default=2, help="re-asks before the bot decides")
    ap.add_argument("--calls", type=int, default=6, help="tool calls per decision (tools)")
    ap.add_argument("--sims", type=int, default=256, help="combats per simulate call")
    ap.add_argument("--env", default=DEFAULT_ENV, help="a file with API_KEY / API_BASE lines")
    ap.add_argument("--key-env", default="OPENAI_API_KEY")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--extra", default="", help="extra request body fields (JSON)")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--read-timeout", type=float, default=300)
    ap.add_argument("--jobs", type=int, default=4, help="games played at once")
    a = ap.parse_args()
    if a.agent in ("llm", "tools") and not a.model:
        ap.error("--model is required for --agent llm/tools")

    out = Path(a.out)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    games = out / "games.jsonl"
    done = {r["seed"] for r in read_rows(games)}
    todo = [s for s in seeds(a.seeds) if s not in done]
    config = json.loads(a.config) if a.config else None
    print(f"{a.agent} {a.model}: {len(todo)} games to play ({len(done)} done)", flush=True)
    lock = threading.Lock()

    def one(seed):
        with open(out / "traces" / f"{seed}.jsonl", "w") as ft:
            def trace(rec):
                ft.write(json.dumps(rec) + "\n")
                ft.flush()
            m = Match(make_agent(a, seed), seed, a.seat, config, a.turn_cap, a.retries,
                      a.max_plan, trace)
            row = m.play()
        row.update(agent=a.agent, model=a.model)
        with lock, open(games, "a") as fg:
            fg.write(json.dumps(row) + "\n")
        print(f"seed {seed}: place {row['placement']} turns {row['turns']} "
              f"decisions {row['decisions']} illegal {row['illegal']} "
              f"fallbacks {row['fallbacks']} {row['seconds']}s ${row['cost_usd']}", flush=True)
        return row

    with cf.ThreadPoolExecutor(max(1, a.jobs)) as ex:
        for f in cf.as_completed([ex.submit(one, s) for s in todo]):
            f.result()

    rows = read_rows(games)
    if rows:
        pl = [r["placement"] for r in rows]
        print(f"{len(rows)} games: mean placement {sum(pl) / len(pl):.2f}, "
              f"top 4 {sum(p <= 4 for p in pl) / len(pl):.0%}, "
              f"1st {sum(p == 1 for p in pl) / len(pl):.0%}")


if __name__ == "__main__":
    main()
