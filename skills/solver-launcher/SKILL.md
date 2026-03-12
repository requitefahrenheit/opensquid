---
name: solver-launcher
description: |
  Given any goal, craft a production-quality solver prompt and launch an autonomous
  agent session on the user's machine. Use this skill whenever the user states a goal
  they want to work on autonomously, says "run this", "launch a session", "kick off
  an agent", "let it run", or describes a problem they want solved in the background.
  Also use when resuming a prior session, checking on running sessions, or requesting
  that a completed session be iterated on. Covers all domains: math/research, writing,
  creative brainstorming, debugging, strategy, planning. Always use this skill rather
  than ad-hoc prompt construction.
compatibility:
  tools: [dev:dev_run, dev:dev_write_file, dev:dev_read_file, dev:dev_patch_file]
  requires: solver.py at ~/solver.py, rwx-server running
---

# Solver Launcher Skill

This skill captures the full workflow for turning a user goal into a running solver
session: domain analysis, prompt crafting, mode/model selection, launch, and monitoring.

## Step 1: Analyze the Goal

Before writing anything, classify the goal across four dimensions:

### 1a. Domain
| Domain | Signals | Primary output |
|--------|---------|----------------|
| **math/research** | prove, conjecture, derive, analyze, find, characterize | theorems, proofs, conjectures with obstruction |
| **writing/creative** | write, draft, screenplay, story, blog, op-ed | text artifacts |
| **brainstorm** | ideas, possibilities, options for a creative problem | idea lists |
| **debugging** | bug, error, fix, 500, crash, failing test | root cause + fix |
| **strategy/decision** | should I, build vs buy, recommend, decide | recommendation + reasons |
| **planning/logistics** | plan, schedule, move, organize, timeline | week-by-week plan |

### 1b. Mode
- **single-thread** — one goal, linear progress, one Worker
- **team-mode** — use when the goal has DISTINCT phases that benefit from
  specialization (generate then critique, research then synthesize, draft then edit)
- **two-phase pipeline** — special case of team: Phase 1 generates raw material,
  Phase 2 reformulates it. Use for: brainstorming, first-draft writing, research
  synthesis. Requires `run_pipeline.sh` (see references/pipeline.md)

**When to use team vs single:**
- Brainstorming → always two-phase pipeline (DRAFTER → CRITIC/REFORMULATOR)
- Research synthesis → team (RESEARCHER → INTEGRATOR)
- Math proofs → always single-thread (iteration defeats parallel)
- Writing drafts → single unless >2000 words or needs fact-checking
- Debugging → single-thread
- Strategy → single-thread unless requires external data (add RESEARCHER)

### 1c. Model
| Model | Use when |
|-------|----------|
| **o3** | math, hard proofs, complex multi-step reasoning, creative work requiring depth |
| **o4-mini** | math on a budget, structured reasoning tasks where speed matters |
| **gpt-4o** | search-heavy tasks, current events, job listings, news synthesis |
| **claude-sonnet-4-20250514** | writing, brainstorm, strategy, planning — best prose quality |

**Note on job search / web-dependent tasks:** gpt-4o with web search produces
unreliable job listings (hallucinated company names, broken links). Avoid using
the solver for job search. Use direct web search instead.

### 1d. Budget (turns)
| Task type | Budget |
|-----------|--------|
| Math proof (open conjecture) | 20 turns |
| Math proof (specific lemma) | 10 turns |
| Brainstorm generation phase | 3 turns |
| Brainstorm reformulation phase | 5 turns |
| Writing draft | 5-8 turns |
| Debugging | 10 turns |
| Strategy/planning | 5 turns |

---

## Step 2: Craft the Prompt

Every prompt has these required sections. See `references/prompt-templates.md` for
domain-specific templates to copy and fill in.

```
## GOAL
[Done condition stated precisely. "Done = X" not "explore X".]

## CONTEXT / PRIOR WORK
[What's established. What has been tried. What failed and why.]

## WHERE TO WORK
[The specific problems or questions to attack, ordered by tractability.]

## METHODOLOGY
### What to do
[Positive instructions specific to this domain.]
### What NOT to do
[Explicit prohibitions. At least 3.]

## FAILED APPROACHES
[Anything already ruled out. Do not retry these.]

## WHAT GOOD OUTPUT LOOKS LIKE
[One bad example and one good example, specific to this domain.]
```

### Domain-specific additions

**Math:** Add before-move declaration requirement, post-proof boundary check, failure
tracking. See `references/math-additions.md`.

**Brainstorm:** Add format spec (problem tag, concrete detail, film technique or
domain reference). Add quota (minimum N ideas). Specify which problems get minimum
coverage first. Include bad/good examples.

**Writing:** Add tone spec, length target, audience. Include a sample passage if
available to anchor voice.

**Debugging:** Include reproduction steps, environment details, what has been tried.

---

## Step 3: Choose Launch Method

### Single-thread
```bash
nohup python3 ~/solver.py \
  --prompt-file ~/prompts/<name>.txt \
  --model <model> \
  --auto \
  --turns <N> \
  --state-file ~/state_<name>.json \
  > ~/log_<name>.txt 2>&1 &
echo "PID: $!"
```

### Resume from state
```bash
nohup python3 ~/solver.py \
  --resume ~/state_<name>.json \
  --model <model> \
  --auto \
  --turns <N> \
  --state-file ~/state_<name>.json \
  >> ~/log_<name>.txt 2>&1 &
```

### Two-phase pipeline
Write `~/run_<name>.sh` using the template in `references/pipeline.md`.
Make executable and launch:
```bash
chmod +x ~/run_<name>.sh
nohup ~/run_<name>.sh > ~/log_<name>_pipeline.txt 2>&1 &
```

---

## Step 4: Monitor

```bash
# Check if running
pgrep -a python3 | grep solver

# Tail any log
tail -30 ~/log_<name>.txt

# Check all sessions at once
for f in ~/log_*.txt; do echo "=== $f ==="; tail -5 $f; done

# Audit log (rwx-server)
tail -20 ~/claude/rwx/audit.log
```

**Reading output:** Look for:
- `[DONE]` — session ended (check if legitimate or premature — see Step 5)
- `REFRAME` — Worker was spinning; check if it recovered
- `CHALLENGE` — goal was wrong; read the restatement
- `ABORT [STOP]` — dead end; requires human input
- `STATE BLOCK` — current state; use for resume

---

## Step 5: Validate Completion

Before reporting [DONE] to the user, check:

1. **Did it meet the stated done condition?** Read the GOAL section of the prompt
   and compare to what was actually produced.
2. **Is the output substantive?** Single-turn [DONE] with low token count ($0.01)
   is almost always premature.
3. **For math:** Is every claim tagged PROVEN, CONJECTURAL, or UNKNOWN? Are boundary
   cases verified? Is the stated obstruction for unproven conjectures explicit?
4. **For brainstorm:** Count the ideas. Did it hit the quota? Are they concrete and
   filmable, or abstract?
5. **For writing:** Does it meet the length and tone spec?

If [DONE] is premature: resume the session. The [DONE] override patch in solver.py
should catch it, but it sometimes doesn't for subtle cases.

---

## Step 6: Common Failure Modes and Fixes

| Failure | Symptom | Fix |
|---------|---------|-----|
| Premature [DONE] | 1 turn, low cost, goal unmet | Resume with `--resume` |
| Spinner | REFRAME 3+ times, no new output | CHALLENGE the goal — it may be wrong |
| Hallucinated output | Job listings with fake company names | Wrong tool for task; do directly |
| Proof not closing | Conjectural with no obstruction named | Ask Worker to name the exact gap |
| Brainstorm too abstract | Ideas describe scenes, don't show them | Add bad/good examples to prompt |
| Team mode not coordinating | Roles talking past each other | Tighten COORDINATOR injections to 2 sentences max |

---

## Reference Files

- `references/prompt-templates.md` — Domain-specific prompt templates (math, brainstorm,
  writing, debugging, strategy)
- `references/math-additions.md` — The three methodology additions for math sessions
  (pre-move declaration, post-proof boundary check, failure tracking)
- `references/pipeline.md` — Two-phase pipeline shell script template
