#!/usr/bin/env python3
"""
Ralph 2 -- Dual-thread autonomous research agent with context management.

Thread A (Worker):   Does the actual mathematical research. Has web search.
Thread B (Evaluator): Reviews Thread A's work, decides what to do next.
Thread C (Context):  Manages context window -- summarizes when things get long.
Narrator:            Writes a plain-language briefing after each round.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  python ralph2.py "Investigate the convergence of the sum of reciprocals of twin primes"
  python ralph2.py --resume session_20260210_143022.json
  python ralph2.py --max-turns 50 --no-test "Explore connections between Collatz and p-adic analysis"

Output files:
  session_*_briefing.md     -- Plain-language summary (read this one!)
  session_*.json            -- Full state (for --resume)
  session_*.log             -- Technical log
  session_*_research_log.md -- Evaluator's cumulative research log
  session_*_transcript.md   -- Full conversation transcript

Session auto-saves after every turn pair. Ctrl-C and --resume later.
"""

import sys
import os
import json
import argparse
from datetime import datetime
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# ANSI color codes
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Thread colors
    A       = "\033[38;5;33m"   # Blue -- Worker
    B       = "\033[38;5;208m"  # Orange -- Evaluator
    C_COLOR = "\033[38;5;141m"  # Purple -- Context Manager
    COST    = "\033[38;5;46m"   # Green -- Money/stats
    WARN    = "\033[38;5;226m"  # Yellow -- Warnings
    ERR     = "\033[38;5;196m"  # Red -- Errors
    OK      = "\033[38;5;46m"   # Green -- Success
    NAR     = "\033[38;5;252m"  # Light gray -- Narrator
    RULE    = "\033[38;5;240m"  # Gray -- Dividers

def _ts():
    """Short timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S")

def log_header(session_goal, models, testing, web_search, turn=0):
    w = 70
    print(f"\n{C.RULE}{'='*w}{C.RESET}")
    print(f"  {C.BOLD}RALPH 2 -- Dual-Thread Research Agent{C.RESET}")
    print(f"  {C.DIM}Goal:{C.RESET} {session_goal}")
    print(f"  {C.A}> A (Worker):{C.RESET}  {models[0]}")
    print(f"  {C.B}> B (Eval):{C.RESET}    {models[1]}")
    print(f"  {C.C_COLOR}> C (Context):{C.RESET} {models[2]}")
    print(f"  {C.DIM}Testing: {testing}  |  Web search: {web_search}  |  Cost limit: ${COST_LIMIT:.0f}{C.RESET}")
    if turn > 0:
        print(f"  {C.WARN}Resuming from turn {turn}{C.RESET}")
    print(f"{C.RULE}{'='*w}{C.RESET}\n")

def log_thread_start(thread, turn, max_turns, label):
    colors = {"A": C.A, "B": C.B, "C": C.C_COLOR}
    color = colors.get(thread, C.RESET)
    icons = {"A": "[W]", "B": "[E]", "C": "[C]"}
    icon = icons.get(thread, ">")
    w = 70
    print(f"\n{color}{'-'*w}")
    print(f"  {icon}  [{_ts()}] TURN {turn}/{max_turns} -- {label} (Thread {thread})")
    print(f"{'-'*w}{C.RESET}\n")

def log_thread_output(thread, text):
    colors = {"A": C.A, "B": C.B, "C": C.C_COLOR}
    color = colors.get(thread, C.RESET)
    prefix = f"{color}|{C.RESET} "
    # Indent each line of output with a colored bar
    for line in text.split("\n"):
        print(f"{prefix}{line}")

def log_thread_stats(thread, in_tok, out_tok, cache_read=0, cache_create=0, searches=0):
    colors = {"A": C.A, "B": C.B, "C": C.C_COLOR}
    color = colors.get(thread, C.RESET)
    parts = [f"in={in_tok:,}", f"out={out_tok:,}"]
    if cache_read or cache_create:
        parts.append(f"cache_read={cache_read:,}")
        parts.append(f"cache_write={cache_create:,}")
    if searches:
        parts.append(f"[web] searches={searches}")
    stats = "  ".join(parts)
    print(f"\n{color}  +- [{_ts()}] {stats}{C.RESET}")

def log_ply_summary(session, turn, max_turns):
    """Print a consistent cost/status block after each A->B ply."""
    cost = session.estimate_cost()
    a_ctx = session.estimate_context_tokens("a")
    b_ctx = session.estimate_context_tokens("b")
    w = 70
    print(f"\n{C.RULE}{'-'*w}{C.RESET}")
    print(f"  {C.COST}[$] Running cost: ${cost:.4f}{C.RESET}   "
          f"{C.DIM}|{C.RESET}   "
          f"Turn {turn}/{max_turns}")
    print(f"  {C.DIM}[#] Tokens: {session.total_input_tokens:,} in / "
          f"{session.total_output_tokens:,} out{C.RESET}")
    print(f"  {C.DIM}[~] Context: "
          f"{C.A}A~{a_ctx:,}tok{C.RESET}  "
          f"{C.B}B~{b_ctx:,}tok{C.RESET}  "
          f"{C.DIM}(compress at {CONTEXT_THRESHOLD:,}){C.RESET}")
    if session.compressions > 0:
        print(f"  {C.C_COLOR}[C]  Compressions: {session.compressions}{C.RESET}")
    print(f"{C.RULE}{'-'*w}{C.RESET}")

def log_done(session, reason="done"):
    w = 70
    cost = session.estimate_cost()
    if reason == "done":
        print(f"\n{C.OK}{'='*w}")
        print(f"  [OK] WORKER DECLARED DONE at turn {session.turn}")
    elif reason == "max_turns":
        print(f"\n{C.WARN}{'='*w}")
        print(f"  [!]  Hit max turns ({session.turn})")
    elif reason == "interrupted":
        print(f"\n{C.WARN}{'='*w}")
        print(f"  [||]  Interrupted at turn {session.turn}")
    elif reason == "error":
        print(f"\n{C.ERR}{'='*w}")
        print(f"  [X] Error at turn {session.turn}")
    print(f"  [$] Final cost: ${cost:.4f}")
    print(f"  [#] Tokens: {session.total_input_tokens:,} in / {session.total_output_tokens:,} out")
    print(f"  [C]  Compressions: {session.compressions}")
    print(f"  [>] Session: {session.session_file}")
    color = {
        "done": C.OK, "max_turns": C.WARN, "cost_limit": C.WARN,
        "interrupted": C.WARN, "error": C.ERR
    }.get(reason, C.RESET)
    print(f"{color}{'='*w}{C.RESET}")

# File logger -- writes plain text (no ANSI) to a log file
_file_logger = None

def init_file_logger(session_file: str):
    global _file_logger
    log_path = session_file.replace(".json", ".log")
    _file_logger = open(log_path, "a", buffering=1)  # line-buffered
    return log_path

def flog(msg: str):
    """Write a line to the file log (no ANSI codes)."""
    if _file_logger:
        # Strip ANSI codes for file output
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', msg)
        _file_logger.write(f"[{_ts()}] {clean}\n")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TESTING = True  # Flip to False for production (Opus + extended thinking)

# Models
if TESTING:
    THREAD_A_MODEL = "claude-haiku-4-5-20251001"
    THREAD_B_MODEL = "claude-haiku-4-5-20251001"
    THREAD_C_MODEL = "claude-haiku-4-5-20251001"
else:
    THREAD_A_MODEL = "claude-opus-4-6"
    THREAD_B_MODEL = "claude-haiku-4-5-20251001"
    THREAD_C_MODEL = "claude-haiku-4-5-20251001"

# Token limits
THREAD_A_MAX_TOKENS = 16384
THREAD_B_MAX_TOKENS = 4096
THREAD_C_MAX_TOKENS = 4096

# Extended thinking (Opus only)
ENABLE_THINKING = not TESTING
THINKING_BUDGET = 10000  # tokens

# Cost limit (USD) -- session stops when estimated cost exceeds this
COST_LIMIT = 10.0

# Context management
CONTEXT_THRESHOLD = 80000  # tokens -- trigger Thread C when A's input exceeds this
# Rough estimate: ~4 chars per token. We count chars and divide by 4.

# Web search
ENABLE_WEB_SEARCH = True
WEB_SEARCH_MAX_USES = 5  # max searches per API call

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

WORKER_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a mathematical research agent. You are working toward a research goal "
            "given to you at the start. Each turn, you do ONE meaningful step: develop a line "
            "of reasoning, prove a lemma, explore a construction, identify an obstruction, etc.\n\n"
            "Guidelines:\n"
            "- Be rigorous. State definitions, assumptions, and claims precisely.\n"
            "- When you're unsure, say so. Distinguish conjecture from proof.\n"
            "- Show your reasoning step by step.\n"
            "- You have access to web search. When the evaluator issues a LITERATURE directive, "
            "or when you need to verify a theorem or find related results, USE the web_search tool "
            "to search for relevant mathematical papers, theorems, and results on arxiv, "
            "MathOverflow, Wikipedia, etc.\n"
            "- If the evaluator suggests a direction, engage with it seriously -- but push "
            "back if you think it's wrong.\n"
            "- At the end of each turn, briefly state what you think the most promising "
            "next step is.\n\n"
            "When you believe the research goal has been fully addressed (theorem proved, "
            "conjecture resolved, or thorough analysis complete), end your message with [DONE].\n"
            "Do NOT say [DONE] prematurely."
        ),
        "cache_control": {"type": "ephemeral"}  # Cache the system prompt
    }
]

EVALUATOR_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a mathematical research evaluator overseeing a worker agent. After each "
            "of the worker's turns, you:\n\n"
            "1. Assess the logical correctness of the work so far.\n"
            "2. Identify any gaps, hidden assumptions, or errors.\n"
            "3. Decide what the worker should do next.\n\n"
            "Your available directives (pick ONE per turn):\n"
            "- CONTINUE: The worker is on a good track. Encourage depth on current line.\n"
            "- REDIRECT [reason]: The current approach seems unproductive. Suggest a new angle.\n"
            "- LITERATURE: Ask the worker to use web search to find relevant known results "
            "(specify what to look for).\n"
            "- RIGOR: The argument is hand-wavy. Ask for precise definitions/proof.\n"
            "- SUMMARIZE: The conversation is getting long. Ask the worker to produce a "
            "clean summary of results so far, dead ends, and open questions.\n"
            "- COUNTEREXAMPLE: Ask the worker to try to find a counterexample or edge case "
            "before claiming a result.\n\n"
            "CRITICAL RULE: If the worker has spent 2+ consecutive turns on literature search "
            "without finding the result it needs, issue REDIRECT. The literature will not solve "
            "every problem. Force the worker to attempt direct proof work: construct arguments, "
            "compute small cases, try proof techniques, or explore a different direction from "
            "the seed document. Research means DOING math, not just reading about it.\n\n"
            "Format your response as:\n"
            "DIRECTIVE: [your chosen directive]\n"
            "FEEDBACK: [your detailed assessment and instructions for the worker]\n\n"
            "Also maintain a running RESEARCH LOG at the end of each of your responses:\n"
            "===RESEARCH_LOG===\n"
            "[Cumulative bullet-point log of: proven results, conjectures, dead ends, open questions]\n"
            "===END_LOG===\n\n"
            "This log persists across turns -- UPDATE it each turn, don't restart it."
        ),
        "cache_control": {"type": "ephemeral"}
    }
]

CONTEXT_MANAGER_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a context management agent for a mathematical research system. "
            "Your job is to compress a long conversation history into a concise but "
            "COMPLETE summary that preserves all mathematically important content.\n\n"
            "You will receive the full conversation history of a research agent. "
            "Produce a summary that includes:\n"
            "1. The original research goal\n"
            "2. All proven results (with proof sketches)\n"
            "3. All conjectures and their current status\n"
            "4. Dead ends and why they failed\n"
            "5. The current line of investigation and where it stands\n"
            "6. Key definitions and notation established\n"
            "7. Open questions\n\n"
            "Be precise and mathematical. Do NOT lose any proven results or key insights. "
            "It is better to be slightly too long than to lose important content.\n\n"
            "Format your output as a clean research summary document."
        ),
        "cache_control": {"type": "ephemeral"}
    }
]

NARRATOR_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You write SHORT plain-language research briefings for someone following along. "
            "They're smart but not a specialist. You're a friend giving them the real talk.\n\n"
            "Write ONE short paragraph (3-5 sentences, MAX 100 words) then a Vibe check line.\n\n"
            "RULES:\n"
            "- ONE paragraph. No headers, no bullets, no markdown formatting.\n"
            "- Say what happened and whether it matters, in plain English.\n"
            "- When you use a math term, explain it in a few words.\n"
            "- Be honest: 'that didn't work' or 'OK this is actually a big deal' as appropriate.\n"
            "- Don't repeat details -- just the gist and why it matters.\n\n"
            "ALWAYS end with exactly one line:\n"
            "Vibe check: [emoji] [one sentence]\n\n"
            "Use (green) for real progress, (yellow) for meh/setup/uncertain, (red) for stuck/dead end.\n\n"
            "Examples of good vibe checks:\n"
            "- Vibe check: (green) That paid off. Real traction now.\n"
            "- Vibe check: (yellow) Necessary housekeeping. Nothing exciting but it needed doing.\n"
            "- Vibe check: (red) Brick wall. Gonna need a different angle.\n\n"
            "KEEP IT SHORT. If your entry is longer than 100 words before the vibe check, "
            "you've written too much. Cut it in half."
        ),
    }
]


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, goal: str, seed: str = ""):
        self.goal = goal
        self.seed = seed             # research seed document (loaded from --seed file)
        self.thread_a_messages = []  # worker conversation
        self.thread_b_messages = []  # evaluator conversation
        self.research_log = ""       # latest research log from evaluator
        self.turn = 0
        self.compressions = 0        # how many times Thread C has compressed
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.created_at = datetime.now().isoformat()
        self.session_file = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    def to_dict(self):
        return {
            "goal": self.goal,
            "seed": self.seed,
            "thread_a_messages": self.thread_a_messages,
            "thread_b_messages": self.thread_b_messages,
            "research_log": self.research_log,
            "turn": self.turn,
            "compressions": self.compressions,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "created_at": self.created_at,
            "session_file": self.session_file,
            "config": {
                "thread_a_model": THREAD_A_MODEL,
                "thread_b_model": THREAD_B_MODEL,
                "thread_c_model": THREAD_C_MODEL,
                "testing": TESTING,
            }
        }

    def save(self):
        with open(self.session_file, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        print(f"  [>] Saved to {self.session_file}")

    @classmethod
    def load(cls, filepath: str) -> "Session":
        with open(filepath) as f:
            data = json.load(f)
        s = cls(data["goal"])
        s.seed = data.get("seed", "")
        s.thread_a_messages = data["thread_a_messages"]
        s.thread_b_messages = data["thread_b_messages"]
        s.research_log = data.get("research_log", "")
        s.turn = data["turn"]
        s.compressions = data.get("compressions", 0)
        s.total_input_tokens = data.get("total_input_tokens", 0)
        s.total_output_tokens = data.get("total_output_tokens", 0)
        s.created_at = data["created_at"]
        s.session_file = filepath
        return s

    def estimate_cost(self):
        """Rough cost estimate based on accumulated tokens."""
        if TESTING:
            input_rate, output_rate = 1.0, 5.0
        else:
            input_rate, output_rate = 5.0, 25.0
        input_cost = (self.total_input_tokens / 1_000_000) * input_rate
        output_cost = (self.total_output_tokens / 1_000_000) * output_rate
        return input_cost + output_cost

    def estimate_context_tokens(self, thread="a"):
        """Rough token count for a thread's messages (~4 chars per token)."""
        messages = self.thread_a_messages if thread == "a" else self.thread_b_messages
        total_chars = sum(
            len(m["content"]) if isinstance(m["content"], str)
            else sum(len(b.get("text", "")) for b in m["content"] if isinstance(b, dict))
            for m in messages
        )
        return total_chars // 4


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def make_cacheable_messages(messages):
    """
    Add cache_control to the last user message in the list.
    This tells Anthropic to cache everything up to and including that point.
    Subsequent calls with the same prefix will get cache hits.
    """
    if not messages:
        return messages

    # Deep copy to avoid mutating the session state
    cached = [dict(msg) for msg in messages]

    # Find the last user message and add cache_control to it
    for i in range(len(cached) - 1, -1, -1):
        if cached[i]["role"] == "user":
            content = cached[i]["content"]
            if isinstance(content, str):
                cached[i]["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"}
                    }
                ]
            elif isinstance(content, list):
                # Add cache_control to the last text block
                cached[i]["content"] = list(content)
                for j in range(len(cached[i]["content"]) - 1, -1, -1):
                    if isinstance(cached[i]["content"][j], dict) and cached[i]["content"][j].get("type") == "text":
                        cached[i]["content"][j] = dict(cached[i]["content"][j])
                        cached[i]["content"][j]["cache_control"] = {"type": "ephemeral"}
                        break
            break

    return cached


def call_worker(client, messages):
    """Call Thread A (worker) with web search and optional extended thinking."""
    kwargs = {
        "model": THREAD_A_MODEL,
        "max_tokens": THREAD_A_MAX_TOKENS,
        "system": WORKER_SYSTEM,
        "messages": make_cacheable_messages(messages),
    }

    # Web search tool
    if ENABLE_WEB_SEARCH:
        kwargs["tools"] = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": WEB_SEARCH_MAX_USES,
        }]

    # Extended thinking
    if ENABLE_THINKING:
        kwargs["temperature"] = 1  # required for extended thinking
        kwargs["thinking"] = {
            "type": "adaptive",
        }

    # Make the call -- may need to loop for pause_turn (web search continuation)
    response = client.messages.create(**kwargs)

    all_content = list(response.content)
    total_in = response.usage.input_tokens
    total_out = response.usage.output_tokens

    # Handle pause_turn: web search may need continuation
    while response.stop_reason == "pause_turn":
        continuation_messages = list(messages)
        continuation_messages.append({
            "role": "assistant",
            "content": response.content
        })
        continuation_messages.append({
            "role": "user",
            "content": "Continue."
        })

        kwargs["messages"] = continuation_messages
        response = client.messages.create(**kwargs)
        all_content.extend(response.content)
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

    # Extract text from all content blocks
    text = ""
    searches_done = 0
    for block in all_content:
        if hasattr(block, "type"):
            if block.type == "text":
                text += block.text
            elif block.type == "web_search_tool_result":
                searches_done += 1

    # Guard against empty text (can happen with adaptive thinking)
    if not text.strip():
        text = "[Worker produced no visible output this turn -- all content was in thinking.]"

    # Track cache performance
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
    cache_create = getattr(response.usage, "cache_creation_input_tokens", 0)

    return text, total_in, total_out, searches_done, cache_read, cache_create


def call_evaluator(client, messages):
    """Call Thread B (evaluator)."""
    response = client.messages.create(
        model=THREAD_B_MODEL,
        max_tokens=THREAD_B_MAX_TOKENS,
        system=EVALUATOR_SYSTEM,
        messages=make_cacheable_messages(messages),
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    if not text.strip():
        text = "DIRECTIVE: CONTINUE\nFEEDBACK: [Evaluator produced no output. Continue current direction.]"
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
    cache_create = getattr(response.usage, "cache_creation_input_tokens", 0)

    return text, response.usage.input_tokens, response.usage.output_tokens, cache_read, cache_create


def call_context_manager(client, thread_a_messages, goal, research_log):
    """Call Thread C to compress Thread A's conversation history."""
    conversation_text = f"RESEARCH GOAL: {goal}\n\n"
    for i, msg in enumerate(thread_a_messages):
        role = "WORKER" if msg["role"] == "assistant" else "INPUT"
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        conversation_text += f"--- {role} (message {i+1}) ---\n{content}\n\n"

    if research_log:
        conversation_text += f"\n--- CURRENT RESEARCH LOG ---\n{research_log}\n"

    messages = [{
        "role": "user",
        "content": (
            "Compress this research conversation into a summary. "
            "Preserve ALL mathematical results and insights.\n\n"
            f"{conversation_text}"
        )
    }]

    response = client.messages.create(
        model=THREAD_C_MODEL,
        max_tokens=THREAD_C_MAX_TOKENS,
        system=CONTEXT_MANAGER_SYSTEM,
        messages=messages,
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    return text, response.usage.input_tokens, response.usage.output_tokens


def extract_research_log(evaluator_response: str) -> str:
    """Pull the research log from the evaluator's response."""
    marker_start = "===RESEARCH_LOG==="
    marker_end = "===END_LOG==="
    if marker_start in evaluator_response and marker_end in evaluator_response:
        start = evaluator_response.index(marker_start) + len(marker_start)
        end = evaluator_response.index(marker_end)
        return evaluator_response[start:end].strip()
    return ""


def call_narrator(client, turn, worker_text, evaluator_text, goal):
    """Call the narrator to produce a plain-language briefing entry."""
    # Truncate inputs to keep narrator costs tiny
    worker_snippet = worker_text[:3000] + ("..." if len(worker_text) > 3000 else "")
    eval_snippet = evaluator_text[:2000] + ("..." if len(evaluator_text) > 2000 else "")

    messages = [{
        "role": "user",
        "content": (
            f"Research goal: {goal}\n\n"
            f"Turn {turn}.\n\n"
            f"WORKER OUTPUT:\n{worker_snippet}\n\n"
            f"EVALUATOR RESPONSE:\n{eval_snippet}\n\n"
            f"Write a brief, plain-language briefing entry for this turn."
        )
    }]

    try:
        response = client.messages.create(
            model=THREAD_B_MODEL,  # Always Haiku -- cheap
            max_tokens=256,
            system=NARRATOR_SYSTEM,
            messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return text, response.usage.input_tokens, response.usage.output_tokens
    except Exception as e:
        return f"(Narrator error: {e})", 0, 0


def write_briefing(session, turn, narrator_text):
    """Append a briefing entry to the human-readable briefing file."""
    base = session.session_file.replace(".json", "")
    briefing_file = f"{base}_briefing.md"

    # Write header on first turn
    if turn == 1:
        with open(briefing_file, "w") as f:
            f.write(f"# [i] Research Briefing\n\n")
            f.write(f"**Goal:** {session.goal}\n\n")
            f.write(f"*What's happening with the AI research agent, explained like "
                    f"you're following along from the couch.*\n\n")
            f.write(f"---\n\n")

    with open(briefing_file, "a") as f:
        f.write(f"### Round {turn}  ({datetime.now().strftime('%H:%M')})\n\n")
        f.write(f"{narrator_text}\n\n")
        cost = session.estimate_cost()
        f.write(f"*Running cost: ${cost:.3f} -- "
                f"Tokens used: {session.total_input_tokens + session.total_output_tokens:,}*\n\n")
        f.write(f"---\n\n")


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def run(session: Session, max_turns: int):
    client = Anthropic()

    # Start file logger
    log_path = init_file_logger(session.session_file)
    flog(f"Session started: {session.goal}")

    log_header(
        session.goal,
        [THREAD_A_MODEL, THREAD_B_MODEL, THREAD_C_MODEL],
        TESTING, ENABLE_WEB_SEARCH, session.turn
    )
    print(f"  {C.DIM}[f] Log file: {log_path}{C.RESET}\n")

    # If fresh start, initialize Thread A with the goal
    if session.turn == 0:
        if session.seed:
            initial_prompt = (
                f"RESEARCH GOAL: {session.goal}\n\n"
                f"RESEARCH SEED DOCUMENT:\n"
                f"{'-'*40}\n"
                f"{session.seed}\n"
                f"{'-'*40}\n\n"
                f"Begin your investigation using the seed document above as context. "
                f"Do one meaningful step."
            )
            seed_tokens = len(session.seed) // 4
            flog(f"Seed document loaded: ~{seed_tokens:,} tokens")
            print(f"  {C.DIM}[+] Seed document: ~{seed_tokens:,} tokens{C.RESET}\n")
        else:
            initial_prompt = (
                f"RESEARCH GOAL: {session.goal}\n\n"
                f"Begin your investigation. Do one meaningful step."
            )
        session.thread_a_messages.append({"role": "user", "content": initial_prompt})

    while session.turn < max_turns:
        session.turn += 1

        # --- Check cost limit ---
        current_cost = session.estimate_cost()
        if current_cost >= COST_LIMIT:
            log_done(session, "cost_limit")
            print(f"  {C.WARN}[$] Cost limit reached: ${current_cost:.2f} >= ${COST_LIMIT:.2f}{C.RESET}")
            flog(f"Cost limit reached: ${current_cost:.2f} >= ${COST_LIMIT:.2f}")
            session.save()
            save_research_output(session)
            return

        # --- Check if Thread C needs to compress ---
        a_tokens = session.estimate_context_tokens("a")
        if a_tokens > CONTEXT_THRESHOLD:
            log_thread_start("C", session.turn, max_turns,
                             f"CONTEXT MANAGER -- Compressing ({a_tokens:,} est. tokens)")
            flog(f"Thread C: Compressing. A context ~{a_tokens:,} tokens")

            summary, in_tok, out_tok = call_context_manager(
                client, session.thread_a_messages, session.goal, session.research_log
            )
            session.total_input_tokens += in_tok
            session.total_output_tokens += out_tok
            session.compressions += 1

            log_thread_output("C", f"Compressed {len(session.thread_a_messages)} messages -> summary")
            log_thread_stats("C", in_tok, out_tok)
            flog(f"Thread C: Compressed {len(session.thread_a_messages)} msgs. "
                 f"in={in_tok}, out={out_tok}")

            # Replace Thread A's history with the compressed summary
            compressed_prompt = (
                f"RESEARCH GOAL: {session.goal}\n\n"
                f"CONTEXT: This is a continuation of an ongoing research investigation. "
                f"Here is a summary of all work done so far (compression #{session.compressions}):\n\n"
                f"{summary}\n\n"
                f"Continue the investigation from where the summary leaves off. "
                f"Do one meaningful step."
            )
            session.thread_a_messages = [{"role": "user", "content": compressed_prompt}]

            # Also compress Thread B if it's getting large
            b_tokens = session.estimate_context_tokens("b")
            if b_tokens > CONTEXT_THRESHOLD // 2:
                eval_compressed = (
                    f"CONTEXT: The worker's conversation has been compressed. "
                    f"Here is the current research log:\n\n{session.research_log}\n\n"
                    f"Continue evaluating the worker's output from here."
                )
                session.thread_b_messages = [{"role": "user", "content": eval_compressed}]
                session.thread_b_messages.append({
                    "role": "assistant",
                    "content": "Understood. I'll continue evaluating from the compressed state."
                })
                log_thread_output("C", "Also compressed Thread B")
                flog("Thread C: Also compressed Thread B")

        # --- Thread A (Worker) turn ---
        log_thread_start("A", session.turn, max_turns, "WORKER")
        flog(f"Thread A: Turn {session.turn} starting")

        worker_text, in_tok, out_tok, searches, cache_read, cache_create = call_worker(
            client, session.thread_a_messages
        )
        session.total_input_tokens += in_tok
        session.total_output_tokens += out_tok

        log_thread_output("A", worker_text)
        log_thread_stats("A", in_tok, out_tok, cache_read, cache_create, searches)
        flog(f"Thread A: in={in_tok}, out={out_tok}, cache_read={cache_read}, "
             f"cache_write={cache_create}, searches={searches}")

        session.thread_a_messages.append({"role": "assistant", "content": worker_text})

        # Check if worker says it's done
        if "[DONE]" in worker_text:
            log_done(session, "done")
            flog(f"Thread A: DONE at turn {session.turn}")
            session.save()
            save_research_output(session)
            return

        # --- Thread B (Evaluator) turn ---
        log_thread_start("B", session.turn, max_turns, "EVALUATOR")
        flog(f"Thread B: Turn {session.turn} starting")

        eval_context = f"WORKER'S OUTPUT (turn {session.turn}):\n{worker_text}"
        if session.research_log:
            eval_context += f"\n\nPREVIOUS RESEARCH LOG:\n{session.research_log}"

        session.thread_b_messages.append({"role": "user", "content": eval_context})

        eval_text, in_tok, out_tok, cache_read, cache_create = call_evaluator(
            client, session.thread_b_messages
        )
        session.total_input_tokens += in_tok
        session.total_output_tokens += out_tok

        log_thread_output("B", eval_text)
        log_thread_stats("B", in_tok, out_tok, cache_read, cache_create)
        flog(f"Thread B: in={in_tok}, out={out_tok}, cache_read={cache_read}, "
             f"cache_write={cache_create}")

        session.thread_b_messages.append({"role": "assistant", "content": eval_text})

        # Extract and persist the research log
        new_log = extract_research_log(eval_text)
        if new_log:
            session.research_log = new_log

        # Feed evaluator's feedback back to the worker
        feedback_for_worker = (
            f"EVALUATOR FEEDBACK:\n{eval_text}\n\n"
            f"Continue your research. Do one meaningful step."
        )
        session.thread_a_messages.append({"role": "user", "content": feedback_for_worker})

        # Save after every turn pair
        session.save()

        # --- Narrator (plain-language briefing) ---
        narrator_text, nar_in, nar_out = call_narrator(
            client, session.turn, worker_text, eval_text, session.goal
        )
        session.total_input_tokens += nar_in
        session.total_output_tokens += nar_out
        write_briefing(session, session.turn, narrator_text)
        print(f"  {C.NAR}[i] Briefing updated{C.RESET}")
        flog(f"Narrator: in={nar_in}, out={nar_out}")

        # --- Ply summary ---
        log_ply_summary(session, session.turn, max_turns)
        flog(f"Ply {session.turn} complete. Cost=${session.estimate_cost():.4f}  "
             f"Tokens: {session.total_input_tokens:,} in / {session.total_output_tokens:,} out")

    # Hit max turns
    log_done(session, "max_turns")
    flog(f"Hit max turns ({max_turns})")
    session.save()
    save_research_output(session)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_research_output(session: Session):
    """Save the final research log and full transcript."""
    base = session.session_file.replace(".json", "")

    # Save research log
    log_file = f"{base}_research_log.md"
    with open(log_file, "w") as f:
        f.write(f"# Research Log\n\n")
        f.write(f"**Goal:** {session.goal}\n\n")
        f.write(f"**Turns:** {session.turn}\n\n")
        f.write(f"**Compressions:** {session.compressions}\n\n")
        f.write(f"**Estimated cost:** ${session.estimate_cost():.4f}\n\n")
        f.write(f"**Tokens:** {session.total_input_tokens:,} in / "
                f"{session.total_output_tokens:,} out\n\n")
        f.write(f"## Log\n\n{session.research_log}\n")
    print(f"  [n] Research log: {log_file}")

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
    print(f"  [t] Transcript: {transcript_file}")

    # Note the briefing file (already written incrementally)
    briefing_file = f"{base}_briefing.md"
    if os.path.exists(briefing_file):
        print(f"  [i] Briefing: {briefing_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ralph 2 -- Dual-thread research agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ralph2.py "Prove that every continuous function on [0,1] is bounded"
  python ralph2.py --seed research_seed.md "Prove D(Cp + C2p2) = 5p - 2"
  python ralph2.py --max-turns 50 "Explore the Riemann hypothesis"
  python ralph2.py --no-test "Serious research with Opus"
  python ralph2.py --no-test --max-cost 20 "Go deeper, spend more"
  python ralph2.py --resume session_20260210_143022.json
  python ralph2.py --no-search "Work without web search"
        """
    )
    parser.add_argument("goal", nargs="?", help="Research goal (short description)")
    parser.add_argument("--seed", type=str,
                        help="Path to a seed document (markdown/text) with detailed context")
    parser.add_argument("--max-turns", type=int, default=20,
                        help="Max turn pairs (default: 20)")
    parser.add_argument("--resume", type=str,
                        help="Resume from a saved session JSON file")
    parser.add_argument("--no-test", action="store_true",
                        help="Production mode: use Opus + extended thinking")
    parser.add_argument("--no-search", action="store_true",
                        help="Disable web search")
    parser.add_argument("--compress-at", type=int, default=CONTEXT_THRESHOLD,
                        help=f"Trigger compression at N tokens (default: {CONTEXT_THRESHOLD})")
    parser.add_argument("--max-cost", type=float, default=COST_LIMIT,
                        help=f"Stop when estimated cost exceeds N dollars (default: {COST_LIMIT})")
    args = parser.parse_args()

    # Apply flags
    if args.no_test:
        TESTING = False
        THREAD_A_MODEL = "claude-opus-4-6"
        ENABLE_THINKING = True
    if args.no_search:
        ENABLE_WEB_SEARCH = False
    CONTEXT_THRESHOLD = args.compress_at
    COST_LIMIT = args.max_cost

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
        log_done(session, "interrupted")
        print(f"  Resume with: python ralph2.py --resume {session.session_file}")
        flog(f"Interrupted at turn {session.turn}")
        session.save()
    except Exception as e:
        log_done(session, "error")
        print(f"  {C.ERR}{e}{C.RESET}")
        print(f"  Resume with: python ralph2.py --resume {session.session_file}")
        flog(f"Error at turn {session.turn}: {e}")
        session.save()
        raise
