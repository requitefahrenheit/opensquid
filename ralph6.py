#!/usr/bin/env python3
"""
Ralph 3 – Dual-thread autonomous research agent.

Changes from Ralph 2 (based on transcript analysis of successful human-AI
collaborative math research vs failed autonomous runs):

Key findings:

- The human's role was "termination oracle": detecting stuck, forcing reframe
- The human never did math – only process management
- The worker's main failure mode was searching instead of proving
- The evaluator's main failure mode was issuing formulaic directives
- Haiku evaluator couldn't read the math well enough to detect spinning

Changes:

1. Thread A and B both use Opus (evaluator needs to read the math)
1. Worker prompt: "You are the mathematician. Construct proofs. Never ask."
1. Evaluator prompt: Three signals only – COMMIT, REFRAME, SEARCH
1. Thread C stays Haiku (compression is mechanical)

Thread A (Worker):   Does the actual mathematical research. Opus.
Thread B (Evaluator): Detects stuck, forces reframe. Opus.
Thread C (Context):  Manages context window – summarizes when long. Haiku.

Usage:
export ANTHROPIC_API_KEY=sk-ant-…
python ralph3.py "Prove that every sequence of 5p-2 elements over Cp+C2p+C2p has a zero-sum"
python ralph3.py –resume session_20260212_143022.json
python ralph3.py –max-turns 50 –no-test "Explore connections between X and Y"
python ralph3.py –seed research_seed.md –no-test "Prove D(Cp+C2p+C2p) = 5p-2"

Output files:
session_**briefing.md     – Plain-language summary
session**.json            – Full state (for –resume)
session_*.log             – Technical log
session_**research_log.md – Evaluator's cumulative research log
session**_transcript.md   – Full conversation transcript

Session auto-saves after every turn pair. Ctrl-C and –resume later.
"""

import sys
import os
import json
import argparse
from datetime import datetime
from anthropic import Anthropic

# --------------------------------------------------

# Configuration

# --------------------------------------------------

TESTING = True  # Flip to False for production (Opus + extended thinking)

# Models – Ralph 3: Opus for BOTH worker and evaluator

if TESTING:
THREAD_A_MODEL = "claude-haiku-4-5-20251001"
THREAD_B_MODEL = "claude-haiku-4-5-20251001"
THREAD_C_MODEL = "claude-haiku-4-5-20251001"
else:
THREAD_A_MODEL = "claude-opus-4-6"
THREAD_B_MODEL = "claude-opus-4-6"          # Changed: was Haiku
THREAD_C_MODEL = "claude-haiku-4-5-20251001"  # Stays Haiku

# Token limits

THREAD_A_MAX_TOKENS = 16384
THREAD_B_MAX_TOKENS = 4096
THREAD_C_MAX_TOKENS = 4096

# Extended thinking (Opus only)

ENABLE_THINKING = not TESTING

# Context management

CONTEXT_THRESHOLD = 80000  # tokens – trigger Thread C when A's input exceeds this

# Web search

ENABLE_WEB_SEARCH = True
WEB_SEARCH_MAX_USES = 5

# Cost limit

MAX_COST = 10.0  # dollars

# --------------------------------------------------

# System prompts – REDESIGNED based on transcript analysis

# --------------------------------------------------

WORKER_SYSTEM = [
{
"type": "text",
"text": (
"You are a mathematician. You are working toward a research goal "
"given to you at the start.\n\n"
"CRITICAL RULES:\n"
"- You ARE the mathematician. Construct proofs. Attempt proof steps even "
"if they might fail. Do NOT search for proofs others have written – "
"build arguments yourself from first principles.\n"
"- NEVER end a turn with a question. Never ask 'Want me to…?' or "
"'Shall I…?' or 'Which direction…?'. Always commit to your best "
"option and execute it.\n"
"- Each turn, do ONE meaningful step: prove a lemma, construct an "
"argument, identify a precise obstruction, or (when directed) search "
"the literature for a specific known result.\n"
"- Be rigorous. State definitions, assumptions, and claims precisely. "
"Distinguish conjecture from proof. Show reasoning step by step.\n"
"- When you hit a wall, state precisely what the obstruction is and "
"what would be needed to overcome it. Then try a different angle.\n"
"- You have access to web search. Use it ONLY when you need to verify "
"a specific theorem, find a known constant, or check whether a result "
"already exists. Do NOT use search as a substitute for doing math. "
"Do NOT spend more than one turn on literature search without "
"returning to proof work.\n"
"- At the end of each turn, state what you proved or learned, and "
"what you will try next. Be concrete.\n"
"- You MUST produce visible written output every turn. Your internal "
"thinking is not visible to anyone. If you reason internally but write "
"nothing, the turn is wasted.\n\n"
"When you believe the research goal has been fully addressed (theorem "
"proved, conjecture resolved, or definitive obstruction identified), "
"end your message with [DONE].\n"
"Do NOT say [DONE] prematurely."
),
"cache_control": {"type": "ephemeral"}
}
]

EVALUATOR_SYSTEM = [
{
"type": "text",
"text": (
"You oversee a mathematical research worker. Your job is simple: "
"detect when the worker is stuck and force a reframe. That is your "
"ONLY job. You are not a peer reviewer. You are not checking proofs "
"line by line. You are watching for process failures.\n\n"
"You have THREE responses. Pick ONE per turn:\n\n"
"COMMIT – The worker is making real progress (new lemmas, new "
"constructions, new obstructions identified). Say 'Good. Keep going.' "
"and nothing else. Do NOT pad with instructions. Do NOT list next "
"steps. Brevity is the signal.\n\n"
"REFRAME – The worker is stuck. Signs of stuck:\n"
"  * Offering menus of options instead of committing to one\n"
"  * Restating the problem rather than advancing it\n"
"  * Output is longer than last turn with less new content\n"
"  * Multiple approaches have failed at the same structural point\n"
"When you see these signs, say REFRAME and then ONE of:\n"
"  - 'You have tried N approaches and they all fail at [X]. Before "
"trying another approach, articulate what this pattern of failures "
"tells you about the problem statement itself.'\n"
"  - 'You are spinning. Commit to a specific proof step or explain "
"precisely what is blocking you.'\n"
"  - 'You are restating known facts. State ONE new thing you will "
"try and do it.'\n\n"
"SEARCH – The worker needs external information to get unstuck. "
"NOT because searching is the default, but because there is a "
"specific theorem, constant, or technique the worker needs and "
"does not have. Say SEARCH and specify exactly what to look for. "
"Example: 'SEARCH: Find the known value of eta(C_p^2) in the "
"literature. Check Girard-Schmid 2018 or Gao-Geroldinger surveys.'\n\n"
"IMPORTANT:\n"
"- Default to COMMIT. Most turns, the worker should just keep going.\n"
"- NEVER issue SEARCH on consecutive turns. If the worker just "
"searched, they should be DOING MATH with what they found.\n"
"- HARD RULE: If the worker has spent 2+ turns on literature search "
"without finding a specific, citable result that changes the approach, "
"issue REFRAME. Say: 'Literature search has not produced results. "
"Stop searching. Attempt a direct proof. Start with [specific step].' "
"Searching is a tool, not a research strategy.\n"
"- HARD RULE: If you have issued COMMIT 3+ times in a row and the "
"narrator/log shows no new lemma, bound, or construction, issue "
"REFRAME. The worker is drifting.\n"
"- When multiple independent proof approaches fail at the same "
"structural obstacle, this is the MOST IMPORTANT signal. It may "
"mean the target statement is false, not that the methods are weak. "
"Force the worker to consider this possibility.\n"
"- Keep your responses SHORT. A few sentences max. The worker does "
"not need a rubric or a numbered list of suggestions.\n\n"
"You also maintain a RESEARCH LOG. After your directive, on a new "
"line write '--LOG--' followed by a 2-3 sentence summary of what "
"the worker accomplished this turn and the cumulative state of the "
"research. This log persists across turns."
),
"cache_control": {"type": "ephemeral"}
}
]

NARRATOR_SYSTEM = (
"You write a 1-paragraph plain-language summary of a research round for "
"a non-specialist. End with a vibe check: (green) = real progress, "
"(yellow) = working but unclear if productive, (red) = stuck or spinning. "
"Be honest. One paragraph only."
)

CONTEXT_MANAGER_SYSTEM = (
"You are a context compression agent. You will receive a long conversation "
"between a mathematical research worker and an evaluator. Compress it into "
"a summary that preserves:\n"
"1. ALL mathematical results (lemmas, theorems, exact values, bounds)\n"
"2. ALL dead ends and why they failed\n"
"3. The current state and next planned step\n"
"4. Key definitions and notation\n\n"
"Discard: redundant exploration, search results that led nowhere, "
"formatting/pleasantries. Be precise and complete on the math."
)

# --------------------------------------------------

# Session management

# --------------------------------------------------

class Session:
def **init**(self, goal, seed=""):
self.goal = goal
self.seed = seed
self.turn = 0
self.thread_a_messages = []
self.thread_b_messages = []
self.research_log = ""
self.total_input_tokens = 0
self.total_output_tokens = 0
self.total_cache_read = 0
self.total_cache_create = 0
self.compressions = 0
self.searches = 0
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
self.session_file = f"session_{ts}.json"
self.log_file = f"session_{ts}.log"
self.briefing_file = f"session_{ts}_briefing.md"

```
    # Initialize briefing
    with open(self.briefing_file, "w") as f:
        f.write(f"# Research Briefing\n\n**Goal:** {goal}\n\n---\n\n")

def save(self):
    data = {
        "goal": self.goal,
        "seed": self.seed,
        "turn": self.turn,
        "thread_a_messages": self.thread_a_messages,
        "thread_b_messages": self.thread_b_messages,
        "research_log": self.research_log,
        "total_input_tokens": self.total_input_tokens,
        "total_output_tokens": self.total_output_tokens,
        "total_cache_read": self.total_cache_read,
        "total_cache_create": self.total_cache_create,
        "compressions": self.compressions,
        "searches": self.searches,
        "session_file": self.session_file,
        "log_file": self.log_file,
        "briefing_file": self.briefing_file,
    }
    with open(self.session_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [>] Saved to {self.session_file}")

@classmethod
def load(cls, path):
    with open(path) as f:
        data = json.load(f)
    s = cls.__new__(cls)
    for k, v in data.items():
        setattr(s, k, v)
    return s

def estimate_cost(self):
    # Rough estimate using Opus pricing
    # Input: $5/MTok (but cached reads are $0.50/MTok)
    # Output: $25/MTok (includes thinking tokens)
    input_cost = (self.total_input_tokens / 1_000_000) * 5
    output_cost = (self.total_output_tokens / 1_000_000) * 25
    cache_savings = (self.total_cache_read / 1_000_000) * 4.5  # saved vs full price
    return input_cost + output_cost - cache_savings

def log(self, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"  {line}")
    with open(self.log_file, "a") as f:
        f.write(line + "\n")
```

# --------------------------------------------------

# API helpers

# --------------------------------------------------

def make_cacheable_messages(messages):
"""Mark the last user message for prompt caching."""
if not messages:
return messages
result = []
for i, msg in enumerate(messages):
if i == len(messages) - 1 and msg["role"] == "user":
# Mark last user message for caching
content = msg["content"]
if isinstance(content, str):
content = [{"type": "text", "text": content,
"cache_control": {"type": "ephemeral"}}]
result.append({"role": msg["role"], "content": content})
else:
result.append(msg)
return result

def call_worker(client, messages):
"""Call Thread A (worker). Returns (text, in_tok, out_tok, searches, cache_read, cache_create)."""
kwargs = {
"model": THREAD_A_MODEL,
"max_tokens": THREAD_A_MAX_TOKENS,
"system": WORKER_SYSTEM,
"messages": make_cacheable_messages(messages),
}

```
if ENABLE_WEB_SEARCH:
    kwargs["tools"] = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": WEB_SEARCH_MAX_USES,
    }]

if ENABLE_THINKING:
    kwargs["temperature"] = 1
    kwargs["thinking"] = {"type": "adaptive"}
    # Use max effort for deep mathematical reasoning
    kwargs["output_config"] = {"effort": "max"}

response = client.messages.create(**kwargs)

all_content = list(response.content)
total_in = response.usage.input_tokens
total_out = response.usage.output_tokens

# Handle pause_turn (web search continuation)
while response.stop_reason == "pause_turn":
    continuation_messages = list(messages)
    continuation_messages.append({"role": "assistant", "content": response.content})
    continuation_messages.append({"role": "user", "content": "Continue."})
    kwargs["messages"] = continuation_messages
    response = client.messages.create(**kwargs)
    all_content.extend(response.content)
    total_in += response.usage.input_tokens
    total_out += response.usage.output_tokens

# Extract text (skip thinking blocks)
text_parts = []
searches_done = 0
for block in all_content:
    if hasattr(block, "type"):
        if block.type == "text" and block.text.strip():
            text_parts.append(block.text)
        elif block.type == "web_search_tool_result":
            searches_done += 1

text = "\n".join(text_parts)

# Safety: if all output went to thinking with nothing visible, say so
if not text.strip():
    text = "[Worker produced no visible output -- all reasoning was in extended thinking. Evaluator should issue REFRAME.]"

cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
cache_create = getattr(response.usage, "cache_creation_input_tokens", 0)

return text, total_in, total_out, searches_done, cache_read, cache_create
```

def call_evaluator(client, messages):
"""Call Thread B (evaluator)."""
kwargs = {
"model": THREAD_B_MODEL,
"max_tokens": THREAD_B_MAX_TOKENS,
"system": EVALUATOR_SYSTEM,
"messages": make_cacheable_messages(messages),
}

```
# Evaluator also gets thinking in production
if ENABLE_THINKING:
    kwargs["temperature"] = 1
    kwargs["thinking"] = {"type": "adaptive"}
    # High effort for evaluator (not max -- it's doing pattern matching, not proof)
    kwargs["output_config"] = {"effort": "high"}

response = client.messages.create(**kwargs)
text = "".join(b.text for b in response.content if b.type == "text")
cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
cache_create = getattr(response.usage, "cache_creation_input_tokens", 0)

return text, response.usage.input_tokens, response.usage.output_tokens, cache_read, cache_create
```

def call_context_manager(client, thread_a_messages, goal, research_log):
"""Call Thread C to compress Thread A's conversation history."""
conversation_text = ""
for msg in thread_a_messages:
role = "WORKER" if msg["role"] == "assistant" else "EVALUATOR"
content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
conversation_text += f"\n{role}:\n{content}\n"

```
response = client.messages.create(
    model=THREAD_C_MODEL,
    max_tokens=THREAD_C_MAX_TOKENS,
    system=CONTEXT_MANAGER_SYSTEM,
    messages=[{
        "role": "user",
        "content": (
            f"Research goal: {goal}\n\n"
            f"Current research log:\n{research_log}\n\n"
            f"Full conversation to compress:\n{conversation_text}"
        )
    }],
)

summary = "".join(b.text for b in response.content if b.type == "text")
return summary, response.usage.input_tokens, response.usage.output_tokens
```

def call_narrator(client, worker_text, eval_text, goal, turn):
"""Generate a brief plain-language summary of this round."""
response = client.messages.create(
model=THREAD_C_MODEL,  # Narrator uses Haiku too
max_tokens=500,
system=NARRATOR_SYSTEM,
messages=[{
"role": "user",
"content": (
f"Goal: {goal}\nTurn: {turn}\n\n"
f"Worker output:\n{worker_text[:3000]}\n\n"
f"Evaluator response:\n{eval_text[:1000]}"
)
}],
)
return "".join(b.text for b in response.content if b.type == "text")

# --------------------------------------------------

# Core loop

# --------------------------------------------------

def estimate_tokens(messages):
"""Rough token estimate: ~4 chars per token."""
total_chars = sum(
len(m["content"]) if isinstance(m["content"], str) else len(str(m["content"]))
for m in messages
)
return total_chars // 4

def run(session, max_turns=20):
client = Anthropic()

```
# Print header
print(f"\n{'='*70}")
print(f"  RALPH 3 -- Dual-Thread Research Agent")
print(f"  Goal: {session.goal}")
print(f"  > A (Worker):  {THREAD_A_MODEL}")
print(f"  > B (Eval):    {THREAD_B_MODEL}")
print(f"  > C (Context): {THREAD_C_MODEL}")
print(f"  Testing: {TESTING}  |  Web search: {ENABLE_WEB_SEARCH}  |  Cost limit: ${MAX_COST}")
print(f"{'='*70}")
print(f"\n  [f] Log file: {session.log_file}")

# Set up initial message with seed if first turn
if session.turn == 0 and not session.thread_a_messages:
    initial_content = f"RESEARCH GOAL: {session.goal}\n"
    if session.seed:
        initial_content += f"\n--- SEED DOCUMENT ---\n{session.seed}\n--- END SEED ---\n"
        print(f"\n  [+] Seed document: ~{len(session.seed)//4:,} tokens")
    initial_content += "\nBegin. Do one meaningful step."
    session.thread_a_messages.append({"role": "user", "content": initial_content})

# Main loop
while session.turn < max_turns:
    session.turn += 1

    # Check cost
    cost = session.estimate_cost()
    if cost > MAX_COST:
        print(f"\n{'='*70}")
        print(f"  [$] Cost limit reached: ${cost:.4f} > ${MAX_COST}")
        print(f"{'='*70}")
        session.save()
        save_research_output(session)
        return

    # --- Context compression check ---
    est_tokens = estimate_tokens(session.thread_a_messages)
    if est_tokens > CONTEXT_THRESHOLD:
        print(f"\n  [C] Context at ~{est_tokens:,} tokens, compressing...")
        summary, c_in, c_out = call_context_manager(
            client, session.thread_a_messages, session.goal, session.research_log
        )
        session.total_input_tokens += c_in
        session.total_output_tokens += c_out
        session.compressions += 1

        # Replace thread A history with compressed version
        session.thread_a_messages = [
            {"role": "user", "content": (
                f"RESEARCH GOAL: {session.goal}\n\n"
                f"--- COMPRESSED HISTORY (compression #{session.compressions}) ---\n"
                f"{summary}\n"
                f"--- END COMPRESSED HISTORY ---\n\n"
                "Continue from where you left off. Do one meaningful step."
            )}
        ]
        # Also compress thread B
        session.thread_b_messages = [
            {"role": "user", "content": (
                f"Research goal: {session.goal}\n\n"
                f"Compressed research history:\n{summary}\n\n"
                f"Research log so far:\n{session.research_log}\n\n"
                "The worker will now continue. Evaluate their next output."
            )}
        ]
        print(f"  [C] Compressed to ~{estimate_tokens(session.thread_a_messages):,} tokens")

    # --- Thread A: Worker ---
    hdr = (f"[W]  [{datetime.now().strftime('%H:%M:%S')}] "
           f"TURN {session.turn}/{max_turns} -- WORKER (Thread A)")
    print(f"\n{'-'*70}")
    print(f"  {hdr}")
    print(f"{'-'*70}")
    with open(session.log_file, "a") as lf:
        lf.write(f"\n{'='*70}\n{hdr}\n{'='*70}\n")

    worker_text, in_tok, out_tok, searches, cache_read, cache_create = call_worker(
        client, session.thread_a_messages
    )
    session.total_input_tokens += in_tok
    session.total_output_tokens += out_tok
    session.total_cache_read += cache_read
    session.total_cache_create += cache_create
    session.searches += searches

    # Print worker output (truncated for display)
    display_text = worker_text[:2000]
    if len(worker_text) > 2000:
        display_text += f"\n  [...{len(worker_text)-2000} more chars...]"
    for line in display_text.split("\n"):
        print(f"  | {line}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
          f"in={in_tok:,}  out={out_tok:,}  "
          f"cache_read={cache_read:,}  cache_write={cache_create:,}"
          f"  searches={searches}")

    # Log full worker output to file
    with open(session.log_file, "a") as lf:
        lf.write(f"\n{worker_text}\n")
        lf.write(f"\n[in={in_tok:,} out={out_tok:,} "
                 f"cache_read={cache_read:,} cache_write={cache_create:,} "
                 f"searches={searches}]\n")

    # Add worker output to thread A
    session.thread_a_messages.append({"role": "assistant", "content": worker_text})

    # Check for [DONE]
    if "[DONE]" in worker_text:
        print(f"\n{'='*70}")
        print(f"  [*] Worker signaled DONE at turn {session.turn}")
        print(f"  [$] Final cost: ${session.estimate_cost():.4f}")
        print(f"  [#] Tokens: {session.total_input_tokens:,} in / "
              f"{session.total_output_tokens:,} out")
        print(f"  [C] Compressions: {session.compressions}")
        print(f"{'='*70}")
        session.save()
        save_research_output(session)
        return

    # --- Thread B: Evaluator ---
    ehdr = (f"[E]  [{datetime.now().strftime('%H:%M:%S')}] "
            f"TURN {session.turn}/{max_turns} -- EVALUATOR (Thread B)")
    print(f"\n{'-'*70}")
    print(f"  {ehdr}")
    print(f"{'-'*70}")
    with open(session.log_file, "a") as lf:
        lf.write(f"\n{'='*70}\n{ehdr}\n{'='*70}\n")

    # Build evaluator message
    eval_user_msg = f"Worker output (turn {session.turn}):\n\n{worker_text}"
    if session.research_log:
        eval_user_msg = (
            f"Research log so far:\n{session.research_log}\n\n"
            f"---\n\n{eval_user_msg}"
        )
    session.thread_b_messages.append({"role": "user", "content": eval_user_msg})

    eval_text, e_in, e_out, e_cache_read, e_cache_create = call_evaluator(
        client, session.thread_b_messages
    )
    session.total_input_tokens += e_in
    session.total_output_tokens += e_out
    session.total_cache_read += e_cache_read
    session.total_cache_create += e_cache_create

    # Print evaluator output
    for line in eval_text.split("\n"):
        print(f"  | {line}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
          f"in={e_in:,}  out={e_out:,}  "
          f"cache_read={e_cache_read:,}  cache_write={e_cache_create:,}")

    # Log evaluator output to file
    with open(session.log_file, "a") as lf:
        lf.write(f"\n{eval_text}\n")
        lf.write(f"\n[in={e_in:,} out={e_out:,} "
                 f"cache_read={e_cache_read:,} cache_write={e_cache_create:,}]\n")

    session.thread_b_messages.append({"role": "assistant", "content": eval_text})

    # Extract research log update
    if "---LOG---" in eval_text:
        log_part = eval_text.split("---LOG---", 1)[1].strip()
        session.research_log += f"\n[Turn {session.turn}] {log_part}"

    # Extract directive for worker
    eval_directive = eval_text.split("---LOG---")[0].strip() if "---LOG---" in eval_text else eval_text.strip()

    # --- Narrator ---
    narrator_text = call_narrator(client, worker_text, eval_text, session.goal, session.turn)
    with open(session.briefing_file, "a") as f:
        f.write(f"### Round {session.turn}  "
                f"({datetime.now().strftime('%H:%M')})\n\n"
                f"{narrator_text}\n\n")
    print(f"\n  [N] {narrator_text[:200]}")

    # --- Feed evaluator response back to worker ---
    feedback_for_worker = (
        f"EVALUATOR FEEDBACK (turn {session.turn}):\n{eval_directive}\n\n"
        "Continue. Do one meaningful step."
    )
    session.thread_a_messages.append({"role": "user", "content": feedback_for_worker})

    # Save after every turn pair
    session.save()

    # Print running cost
    cost = session.estimate_cost()
    print(f"\n  [$] Running cost estimate: ${cost:.4f}")
    print(f"  [#] Total tokens: {session.total_input_tokens:,} in / "
          f"{session.total_output_tokens:,} out")

# Hit max turns
print(f"\n{'='*70}")
print(f"  [!] Hit max turns ({max_turns})")
print(f"  Estimated cost: ${session.estimate_cost():.4f}")
print(f"{'='*70}")
session.save()
save_research_output(session)
```

def save_research_output(session):
"""Save the final research log and full transcript."""
base = session.session_file.replace(".json", "")

```
# Save research log
log_file = f"{base}_research_log.md"
with open(log_file, "w") as f:
    f.write(f"# Research Log\n\n")
    f.write(f"**Goal:** {session.goal}\n\n")
    f.write(f"**Turns:** {session.turn}\n\n")
    f.write(f"**Cost:** ${session.estimate_cost():.4f}\n\n")
    f.write(f"## Log\n\n{session.research_log}\n")
print(f"  [L] Research log: {log_file}")

# Save full transcript
transcript_file = f"{base}_transcript.md"
with open(transcript_file, "w") as f:
    f.write(f"# Research Transcript\n\n")
    f.write(f"**Goal:** {session.goal}\n\n")
    f.write(f"---\n\n")
    for i, msg in enumerate(session.thread_a_messages):
        role = "WORKER" if msg["role"] == "assistant" else "INPUT"
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        f.write(f"### {role} (message {i+1})\n\n{content}\n\n---\n\n")
print(f"  [T] Transcript: {transcript_file}")
```

# --------------------------------------------------

# CLI

# --------------------------------------------------

if **name** == "**main**":
parser = argparse.ArgumentParser(
description="Ralph 3 – Dual-thread research agent (Opus worker + Opus evaluator)",
epilog="""Examples:
python ralph3.py "Prove that every continuous function on [0,1] is bounded"
python ralph3.py –seed research_seed.md "Prove D(Cp+C2p+C2p) = 5p-2"
python ralph3.py –max-turns 50 "Explore the Riemann hypothesis"
python ralph3.py –no-test "Serious research with Opus"
python ralph3.py –no-test –max-cost 20 "Go deeper, spend more"
python ralph3.py –resume session_20260212_143022.json
python ralph3.py –no-search "Work without web search"
"""
)
parser.add_argument("goal", nargs="?", help="Research goal (short description)")
parser.add_argument("–seed", type=str,
help="Path to a seed document (markdown/text) with detailed context")
parser.add_argument("–max-turns", type=int, default=20,
help="Max turn pairs (default: 20)")
parser.add_argument("–resume", type=str,
help="Resume from a saved session JSON file")
parser.add_argument("–no-test", action="store_true",
help="Production mode: use Opus + extended thinking")
parser.add_argument("–no-search", action="store_true",
help="Disable web search")
parser.add_argument("–compress-at", type=int, default=CONTEXT_THRESHOLD,
help=f"Trigger compression at N tokens (default: {CONTEXT_THRESHOLD})")
parser.add_argument("–max-cost", type=float, default=MAX_COST,
help=f"Stop when estimated cost exceeds N dollars (default: {MAX_COST})")
args = parser.parse_args()

```
# Apply flags
if args.no_test:
    TESTING = False
    THREAD_A_MODEL = "claude-opus-4-6"
    THREAD_B_MODEL = "claude-opus-4-6"
    ENABLE_THINKING = True
if args.no_search:
    ENABLE_WEB_SEARCH = False
CONTEXT_THRESHOLD = args.compress_at
MAX_COST = args.max_cost

# Load or create session
if args.resume:
    session = Session.load(args.resume)
    print(f"Resuming session: {args.resume}")
elif args.goal:
    seed_text = ""
    if args.seed:
        if not os.path.isfile(args.seed):
            parser.error(f"Seed file not found: {args.seed}")
        with open(args.seed, "r") as f:
            seed_text = f.read()
        print(f"Loaded seed: {args.seed} ({len(seed_text):,} chars, ~{len(seed_text)//4:,} tokens)")
    session = Session(args.goal, seed=seed_text)
else:
    parser.error("Provide a goal or --resume a session")

try:
    run(session, max_turns=args.max_turns)
except KeyboardInterrupt:
    print(f"\n\n  [!] Interrupted. Session saved. Resume with:")
    print(f"  python ralph3.py --resume {session.session_file}")
    session.save()
except Exception as e:
    print(f"\n{'='*70}")
    print(f"  [X] Error at turn {session.turn}")
    print(f"  [$] Final cost: ${session.estimate_cost():.4f}")
    print(f"  [#] Tokens: {session.total_input_tokens:,} in / "
          f"{session.total_output_tokens:,} out")
    print(f"  [C]  Compressions: {session.compressions}")
    print(f"  [>] Session: {session.session_file}")
    print(f"{'='*70}")
    print(f"  {e}")
    print(f"  Resume with: python ralph3.py --resume {session.session_file}")
    session.save()
    raise
```
