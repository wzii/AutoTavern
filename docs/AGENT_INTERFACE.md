# Writing an agent for TavernGym games

[Back to AutoTavern](../README.md) · [LLM play](LLM_PLAY.md) · [TavernGym](TAVERNGYM.md)

An agent plays one seat of a complete eight-player game. It can be rules, a search, a language
model, or a mix of these. One method is all the code needs:

```python
def decide(self, decision: Decision) -> Plan: ...
```

The same agent then runs everywhere without changes:

- in batch games against the bots (`python -m tavernbench.play --agent custom`);
- in the browser lobby with people and other agents (`python -m tavernbench.play.serve`);
- in the replays and step history, which show what it did and why.

This page is the contract between the agent and the game. `examples/greedy_agent.py` in
`tavernbench/` is a complete, runnable agent (deliberately weak: it shows the interface).

## 1. The loop

```text
game ── needs a decision from your seat ──> Decision ──> agent.decide() ──> Plan
  ^                                                                          │
  └──── executes the plan's actions in order, stops early when needed ◄──────┘
```

The other seats act on their own, as in the game: recruit turns are simultaneous, and combat
starts when every living player has ended the turn. The game asks your agent again whenever
your seat needs an action. An agent object is used for one game; keep any memory you want
across decisions in the object itself.

## 2. `Decision`: what the agent gets

`tavernbench/tavernbench/play/driver.py`

| field | type | content |
| :--- | :--- | :--- |
| `view` | dict | The seat's player view as JSON (section 4). It holds everything the player may know. |
| `state_text` | str | The same view as text, as the bundled language-model agents see it. |
| `legal` | list[int] | The legal action indices now. The first action of a plan must be one of these. |
| `actions_text` | str | One `[index] description` line per legal action, plus the "after buying" entries (section 3). |
| `turn_log` | list[str] | The actions already executed this turn, oldest first. |
| `feedback` | str | Why the previous plan stopped. Examples: an illegal index, "new cards were revealed", "a choice is pending", or the end-of-turn check. |
| `notes` | str | The memo your previous `Plan` returned. Empty at the start. |
| `seat` | int | Your seat (0-7). |
| `game` | Game | The live game. **Read only**: never `step` it. Work on `game.copy()` or `agents.reseeded_copy(game, salt)` (section 6). |

## 3. `Plan`: what the agent returns

```python
Plan(actions: list[int], notes: str | None = None, info: dict = {})
```

**`actions`** are action indices, executed in order (at most `--max-plan`, default 10).

- **Indices keep their meaning.** Every index means what it meant in `legal` and
  `actions_text` when you wrote the plan. The game follows each card by its identity (Tavern
  card, hand card, warband minion) as positions change. `[play hand 0 at position 0, sell
  warband 0]` therefore sells the minion that was leftmost before the play, not the one just
  played. Never adjust indices for earlier actions in the plan.
- **Playing a card bought in the same plan.** The card joins the hand at position
  `len(hand)`. `actions_text` lists these entries apart, under "After buying in the same plan,
  the first card you buy", and "the second" when the Gold allows two buys:
  `[buy Tavern 2, play the first card you buy into position 3, end turn]`. They are not
  legal as the first action.
- **The plan stops early, and the agent is asked again, when:**
  - an action is not legal (`feedback` says which);
  - something new is revealed: a refresh, a Hero Power or Dark Gift, a pending choice
    (Discover, target, Choose One, trinket, hero), or a card you did not buy appearing in hand
    or in the Tavern;
  - the end-of-turn check stops an End Turn (below);
  - the turn ends.
- **Cards that move within a plan.** A card gets a new identity when it changes zone. A
  minion played in this plan is not on the warband your indices describe, so "play it, then
  Activate it" takes two decisions. Buy-then-play is the exception: it has its own entries.

**The end-of-turn check.** When the plan sends End Turn (0) while a minion in hand could still
be played, or Gold could still buy, upgrade, activate a minion, use the Hero Power, take a
Dark Gift or refresh, the turn does not end. `feedback` names what is left. The check asks
again only when a new reason appears in the same turn, so sending `[0]` again ends the turn.

**Limits.** An agent that has not ended its turn after `--turn-cap` actions (default 40) has
it ended. After `--retries` replies without a legal first action (default 2), the scripted bot
takes one action for the seat. Both are counted in the results (`forced_ends`, `fallbacks`).

**`notes`** replaces the memo that comes back in the next `Decision`. Use it for anything
that must survive between decisions, for example when your agent calls a model without
history.

**`info`** is stored with the step and shown in the replays and the lobby's History panel.
Fill whichever of these fields apply; other keys are stored as they are.

| key | content |
| :--- | :--- |
| `prompt` | what your model was asked (the full text) |
| `response` | its raw reply |
| `reasoning` | a short explanation of the plan (quoted under each step in the replays and the History panel) |
| `thinking` | the model's thinking, if your API returns it |
| `tools` | a list of `{"tool", "args", "result"}` |
| `cost_usd`, `input_tokens`, `output_tokens`, `tool_calls` | summed per game in `games.jsonl` |
| `error` | set it, with an empty plan, when your agent failed: the bot takes one action for the seat (an exception in `decide` does the same) |

`agents.record_reply(info, prompt, text, thinking)` fills the first four fields from a reply
that contains `{"reasoning": ..., "actions": [...], "notes": ...}`.

## 4. The view

`view` is `Game.public_view(seat)`: what this player may know. It never contains another
player's current warband, or combats this player did not fight. The main keys:

| path | content |
| :--- | :--- |
| `turn`, `phase` | `hero_select` or `recruit` |
| `alive`, `lobby_races`, `paradox` | the lobby: players left, tribes in play, a lobby-wide Paradox card |
| `me.hero`, `me.hero_powers[]` | hero; each power with `price`, `usable`, `passive`, live `text` |
| `me.health`, `me.armor`, `me.gold`, `me.max_gold`, `me.tier` | |
| `me.upgrade_price`, `me.refresh_price`, `me.minion_price`, `me.free_refreshes`, `me.frozen` | |
| `me.board[]`, `me.hand[]`, `me.shop[]` | cards: `name`, `atk`, `hp`, `tier`, `races`, `keywords`, `golden`, `text` (with current numbers), `attached` (magnetic), `activate_cost`. Board minions have `eid`; hand cards have `hid` and `type`; Tavern cards have `sid`, `type`, `price`, `frozen`, and `owned` / `triple_need` |
| `me.pending` | the choice to make: `kind` (`hero`, `discover`, `target`, `choose_one`, `trinket`, `hero_power`, `quest`, `timewarp`), `options[]` or `targets[]` (entity ids) |
| `me.trinkets`, `me.quests`, `me.rewards`, `me.secrets`, `me.deity`, `me.effects`, `me.scheduled` | |
| `me.dark_gift` | `available`, `cost`, `tiers` now and `next_tiers` |
| `me.turn_events` | what happened at the start of this turn (engine log lines) |
| `me.next_opponent`, `me.last_opponent`, `me.next_is_ghost`, `me.win_streak`, `me.last_damage` | |
| `opponents[]` | per player: `hero` (with `hero_power_id`), `health`, `armor`, `tier`, `alive`, `placement`, `win_streak`, `trinkets`, `quests`, `last_warband` (tribe counts), `seen_board` and `seen_turn` (the warband this player saw when it last fought them), `is_next_opponent` |

For a static card's text (an opponent's Hero Power, say), use
`taverngym.catalog_json()` (all cards by `id`). The last combat, as the player watched it,
comes from `view.own_combats(game, seat, detail=True)`: both warbands at the start and every
event. `state_text` shows all of this.

`Game.view(seat)` is the debugging view and contains hidden information. An agent must not
use it.

## 5. Actions

245 indices (`taverngym.action_name(a)` decodes one; `actions.describe(a, view)` describes it
for the current state):

| range | action |
| :--- | :--- |
| 0 / 1 / 2 / 3 | end turn / refresh / freeze (or unfreeze) / upgrade the Tavern |
| 4-5 | Hero Power (slot 0, 1) |
| 6 / 7 | Dark Gift / back out of a target choice |
| 8-15 | buy Tavern card 0-7 |
| 16-22 | sell warband minion 0-6 |
| 23-29 | activate warband minion 0-6 |
| 30-45 | choose option 0-15 of the pending choice (for `target`, the index into `targets`) |
| 46-125 | play hand card h (0-9) at warband position p (0-7): `46 + 8h + p`; a spell uses p = 0 |
| 126-195 | magnetize hand card h onto warband minion j: `126 + 7h + j` |
| 196-244 | move warband minion i to position j: `196 + 7i + j` |

Constants are in `tavernbench.play.actions` (`A.BUY`, `A.PLAY`, `A.N_POS`...).

## 6. Looking ahead without cheating

Tools may work on copies of the game. A copy made with `game.copy()` keeps the game's random
numbers, so stepping it would show the real next refresh or Discover. Use
`agents.reseeded_copy(game, salt)` for anything that draws cards.

| helper (`tavernbench.play.agents`) | what it does |
| :--- | :--- |
| `reseeded_copy(game, salt)` | a copy with new random numbers |
| `apply_plan(copy, seat, plan)` | applies a plan to a copy exactly as the game would (identity tracking included); returns an error message or None |
| `simulate(copy, seat, n)` | fights the next combat n times against the warband you last saw your next opponent play (not their current one); returns win / tie / loss and expected damage |
| `actions.legal(copy, seat)`, `view.public_view(copy, seat)`, `view.render(...)` | the same information on the copy |

`ToolAgent` in `agents.py` wraps two of these as function-calling tools for a language model.

## 7. Running it

Put your agent in an importable module (the working directory is on the path):

```bash
cd tavernbench
# batch games against the bots, resumable; results, traces and run.json in runs/mine
python -m tavernbench.play --agent custom --agent-class my_agent:MyAgent \
    --agent-args '{"depth": 2}' --seeds 0-49 --jobs 4 --out runs/mine
# a replay page of game 3
python -m tavernbench.play.replay --run runs/mine --seed 3
# play against it in the browser (you in seat 0, your agent in seats 1 and 2)
python -m tavernbench.play.serve --humans 0 --llm 1,2 --llm-agent custom \
    --agent-class my_agent:MyAgent --label "My agent"
```

In Python:

```python
from tavernbench.play.driver import Match
row = Match(MyAgent(), seed=7, trace=print).play()   # placement, turns, decisions, cost...
```

The baselines: `--agent bot` (the engine's scripted bot: 4.4 mean placement over 200 games
against itself, by symmetry about 4.5), `--agent random` (about 7.6), and the bundled
language-model agents `--agent llm` and `--agent tools`. A scripted bot plays every other
seat, so results compare an agent with that bot; seven bots are a weak lobby.

## 8. Checklist

- Return at least one action. When a choice is pending, choose (30+) or back out (7).
- End your turns (0). Unspent Gold is lost, and minions in hand do not fight.
- Do not step `decision.game`, and do not read `Game.view()`.
- Fill `info["reasoning"]` (and `prompt` / `response` for a model) so the replays explain
  your agent.
- Results vary a lot between games: compare agents over the same seeds, and over 50 games or
  more.
