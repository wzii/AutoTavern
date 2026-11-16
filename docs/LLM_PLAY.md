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
Pass model-specific request fields with `--extra '{...}'`. With `--backend claude-cli
--model sonnet`, Claude is called through the signed-in Claude Code CLI instead (bare play
only; no MCP servers are loaded, which would otherwise add 100k+ tokens to every call). The
output is:

- `games.jsonl`: one row per game. It holds the placement, the agent's last turn, decisions,
  actions, illegal actions, fallbacks to the bot, turns ended by the cap, tokens and tool calls.
- `traces/<seed>.jsonl`: one row per decision, in the record format below.
- `run.json`: what every decision shares: the system prompt, the model and the settings.

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
- it ends the turn while a minion in hand could still be played, or while Gold could still
  buy, upgrade or use the Hero Power (asked once per turn; a second `0` ends it). A model that
  writes "buy X and play it" often sends `[buy, 0]`, since the play action of a card not yet
  in hand is not in the list it sees;
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

## Step records

Every decision is stored with what went in and what came out, in the same format for batch
runs (`traces/<seed>.jsonl`) and the browser lobby (`history.jsonl`):

| field | content |
| :--- | :--- |
| `seat`, `turn`, `phase`, `time` | where and when |
| `state` | the visible state as text, as the model saw it |
| `feedback` | what the driver told the model first (an illegal action, the end-of-turn check) |
| `plan`, `did`, `stop` | the action indices it sent, what was executed, and why the plan stopped |
| `notes` | the memo it kept for later turns |
| `prompt`, `response` | the full user prompt and the raw reply |
| `reasoning` | the reply's `reasoning` field |
| `thinking` | the model's thinking where the API returns it (`reasoning_content`); the Claude Code CLI returns thinking blocks without text, so it is empty there |
| `tools` | tool calls with arguments and results (`--agent tools`) |
| `cost_usd`, `input_tokens`, `output_tokens`, `latency_s` | cost |

In a trace the fields from `prompt` on sit under `info`; the lobby's history flattens them.
An agent of your own gets the same storage and display by putting them in the `info` of its
`Plan` (`agents.record_reply(info, prompt, text, thinking)` fills the usual ones).

## Replays

```bash
python -m tavernbench.play.replay --run runs/MODEL --seed 0      # -> runs/MODEL/replays/0.html
```

A game is determined by its seed and the agent's plans, so the replay plays the recorded plans
again (no model is called), checks the result against the original game, and writes one
self-contained page. It shows every turn: the state at each decision (Tavern, warband, hand,
pending choice, lobby), the model's reasoning, raw reply, thinking and tool calls, the actions
executed, the recruit phase one action at a time (what each refresh, Discover or effect
brought is outlined), and each combat step by step.

## Playing against the models

```bash
python -m tavernbench.play.serve --humans 0 --llm 1,2 --backend claude-cli --model sonnet
```

This opens a lobby at http://localhost:8765/ with you in seat 0, the model in seats 1 and 2 and
bots elsewhere (`--humans 0,1` for two people: each opens `/?seat=k`; `--host 0.0.0.0` for
other machines; seats are not password-protected). Recruit turns run at the same time, as in
the game: models think in the background, and combat starts when everyone has ended the turn.

Click a Tavern card to buy it; click a hand card, then a gap in the warband to place it; click
a warband minion to sell, activate or move it, or drag it. Choices (hero, Discover, trinkets,
targets) open as a dialog, and the last combat replays on the right. Ending a turn with an
unplayed minion or spendable Gold asks for a second click. The History panel lists your steps
and, after the game, every model's steps with their full output (`--show-thoughts` shows them
live; they can reveal what that player holds). The history is also written to
`runs/lobby/<seed>-<time>/history.jsonl`.

## Writing your own agent

The full contract (what an agent gets, what it returns, the action space, tools on copies,
running it in batches and in the lobby) is in [Writing an agent](AGENT_INTERFACE.md).

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
