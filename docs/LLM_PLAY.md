# Playing full games with a language model

[Back to AutoTavern](../README.md) · [TavernGym](TAVERNGYM.md) · [TavernBench](TAVERNBENCH.md)

TavernBench asks single questions about fixed positions. `tavernbench.play` lets a language
model play complete eight-player TavernGym games instead: one seat for the model, seven seats
for the scripted bots. A game's result is the model's final placement.

## Run

Install from the repository root with the runner dependencies, then run from `tavernbench/`:

```bash
pip install ".[bench]"
cd tavernbench
# a bare model: one call per decision
python -m tavernbench.play --agent llm --model MODEL --seeds 0-19 --out runs/MODEL \
    --base-url https://api.example.com/v1 --key-env API_KEY
# a model with tools that try plays in the engine before it answers
python -m tavernbench.play --agent tools --model MODEL --seeds 0-19 --out runs/MODEL-tools \
    --calls 6 --sims 256
# baselines, no model needed
python -m tavernbench.play --agent bot --seeds 0-99 --out runs/bot
python -m tavernbench.play --agent random --seeds 0-99 --out runs/random
```

The key and gateway options follow `run_openai`. Any OpenAI-compatible chat endpoint works.
Pass model-specific request fields with `--extra '{...}'`. The output is:

- `games.jsonl`: one row per game. It holds the placement, the agent's last turn, decisions,
  actions, illegal actions, fallbacks to the bot, turns ended by the cap, tokens and tool calls.
- `traces/<seed>.jsonl`: one row per decision. It holds the plan, how much of it ran and why it
  stopped, the reply and the tool calls.

A run can be resumed: seeds already in `games.jsonl` are skipped. The run ends with the mean
placement and the top-4 and first-place rates. A scripted bot against seven bots places 4.5 on
average by symmetry.

## What the model sees

Each decision is one fresh request with three parts:

- **System prompt**: the rules summary from TavernBench and the answer format.
- **User message**, made of:
  - the player's memo (`notes`);
  - the visible state as text: hero, Health, Gold, prices, Hero Power, trinkets, quests,
    warband, hand, Tavern (each card with its current text), any pending choice, the lobby, and
    the result of the last combat;
  - the actions already taken this turn;
  - the numbered legal actions, for example `[9] buy Tavern 1: Suspicious Prisonguard (3 Gold)`.
- **Answer**: `{"reasoning": "...", "actions": [9, 46, 0], "notes": "..."}`.

The state is built from `Game.public_view`. It contains only what the seat may know: each
opponent's hero, Health, tier, streak and the tribes of their last warband, plus the warband
the player saw the last time it fought them. It never contains another player's current board,
or combats the player did not fight. (`Game.view` is the debugging view and shows everything.)

## How a plan runs

The driver executes the listed actions in order. It asks the model again when one of these
happens:

- an action is illegal (the model is told which one);
- an action reveals something new: a refresh, a Hero Power, a Dark Gift, a pending choice
  (Discover, target, Choose One, trinket), or a card that was not bought appearing in hand or
  in the Tavern;
- the turn ends.

Only the first index is checked against the list the model saw. Each later index is read in
the state after the earlier actions, and the prompt explains how positions shift. If a reply
gives no legal first action after `--retries` re-asks, the bot takes that action.
`--turn-cap` (default 40) ends a turn the model does not end itself; moving minions and
freezing never advance the game. `--max-plan` limits the actions run from one reply.

## Tools (`--agent tools`)

Before it answers, the model may call two tools, at most `--calls` times per decision:

| tool | effect |
| :--- | :--- |
| `inspect(actions)` | Applies the indices to a copy of the game and returns the new state and legal actions. |
| `simulate(actions)` | Does the same, then fights the next combat `--sims` times against the warband the player last saw its next opponent play. Returns win, tie and loss chances, expected damage, and the turn that warband was seen. |

Tools run on copies with fresh random numbers. A refresh or Discover inside a tool shows a
possible outcome, not the real one, so the model cannot look ahead.

## Writing your own agent

An agent is any object with `decide(decision) -> Plan`. The `Decision` holds the live `game`
(read it or work on copies only), `seat`, `view`, `state_text`, `legal`, `actions_text`,
`turn_log`, `feedback` and `notes`. The `Plan` holds the action indices, an optional new memo
and an `info` dict that is logged.

```python
from tavernbench.play.driver import Match, Plan

class FirstLegal:
    def decide(self, d):
        return Plan([d.legal[0]])

print(Match(FirstLegal(), seed=1).play())
```

The same pieces work for an agent framework of your own. `view.public_view` and `view.render`
give the state, `actions.listing` the legal actions, and `agents.simulate` the combat preview
against the seen warband.
