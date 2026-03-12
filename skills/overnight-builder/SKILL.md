---
name: overnight-builder
description: >
  Use this skill when the user wants to build something while they sleep, run
  an overnight session, queue up autonomous work, or says "build this overnight",
  "run this while I sleep", "kick off an overnight session", or "queue up tasks
  for later". Reads goals from a goals.txt file, generates concrete tasks, and
  spawns a build session. Also use when checking on overnight results.
---

# Overnight Builder Skill

Turns a list of goals into a structured overnight work plan, then executes it
as an autonomous dispatcher session.

## When to Use

- User says "build this overnight", "run while I sleep", "queue this up"
- User wants to plan work to happen autonomously without supervision
- A goals.txt file exists at ~/claude/overnight/goals.txt

## Step 1: Read the Goals

Check for ~/claude/overnight/goals.txt. If it exists, read it. If not, use the
user's stated goal directly.

```
cat ~/claude/overnight/goals.txt
```

## Step 2: Generate the Work Plan

From the goals, produce a concrete work plan with 4-5 tasks. Rules:
- At least ONE task must be a tangible build artifact (code, document, plan file)
- Tasks should be ordered by dependency (easy/research first, build last)
- Each task should be completable in 5-10 agent turns
- No task should require human input mid-execution

Format:
```
OVERNIGHT BUILD PLAN — {date}

GOALS:
- {goal 1}
- {goal 2}

TASKS:
1. [RESEARCH] {concrete research task with specific output}
2. [ANALYZE] {analysis task with specific deliverable}
3. [DRAFT] {draft/outline task}
4. [BUILD] {the main build task — produces a real artifact}
5. [WRITE-UP] {summary/result synthesis task}

SESSION CONFIG:
- arch: claude
- model: claude-sonnet-4-6
- max_turns: 30
- budget: $8.00
- output: ~/claude/overnight/results/{date}/
```

Write this plan to ~/claude/overnight/plans/{date}.md.

## Step 3: Synthesize into a Single Session Goal

Combine all tasks into one compound goal for the dispatcher session. The agent
will work through them sequentially, checking off each one.

Example compound goal:
```
You are an overnight build agent. Work through these tasks in order, producing
real output for each one. Write results to ~/claude/overnight/results/{date}/.

TASK 1: {task 1 description}
Done when: {specific deliverable exists}

TASK 2: {task 2 description}
Done when: {specific deliverable exists}

[...]

When ALL tasks are done, write a summary to ~/claude/overnight/results/{date}/SUMMARY.md
listing what was accomplished and what still needs human review.

[DONE] only when all tasks are complete and SUMMARY.md is written.
```

## Step 4: Launch the Session

POST to dispatcher:
```bash
curl -X POST http://localhost:8255/run?token=emc2ymmv \
  -H 'Content-Type: application/json' \
  -d '{
    "goal": "<compound goal from step 3>",
    "arch": "claude",
    "max_turns": 30,
    "budget": 8.0
  }'
```

Save the session_id returned. Write it to ~/claude/overnight/active-session.txt.

## Step 5: Report to User

Tell the user:
- What tasks are queued
- The session ID for monitoring
- How to check progress: `curl http://localhost:8255/status/{session_id}?token=emc2ymmv`
- Where results will be written

## Step 6: Checking Results (next morning)

When the user asks "what happened overnight" or "check overnight results":

1. Read ~/claude/overnight/active-session.txt for the session ID
2. GET /status/{session_id} to check completion status
3. Read ~/claude/overnight/results/{date}/SUMMARY.md
4. Report: tasks completed, any failures, cost, artifacts produced

## Output Format for Cortex

On completion, the dispatcher automatically stores results in Cortex tagged:
`[dispatcher, {session_id}, overnight-builder, overnight-build]`

Search for them with: `overnight-build` in Cortex.
