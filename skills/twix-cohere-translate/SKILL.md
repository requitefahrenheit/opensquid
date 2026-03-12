---
name: twix-cohere-translate
description: >
  How to run batch translation jobs using twix decode with the Cohere LLM server
  on the LW research cluster. Use this skill whenever Jeremy wants to translate
  a text file using twix, run a decode job, use an LP config, translate with Cohere,
  or run any batch translation on gpu-a6002 or sdeneefe-g7. Also use when setting
  up new translation jobs, checking job status, or debugging twix decode errors.
---

# Twix Cohere Translation Skill

Runs batch translation jobs via `twix decode` using Cohere-backed LP configs against
the internal LLM server. Everything runs on **gpu-a6002** as user `jfischer`.

## Key Facts

- **Machine:** gpu-a6002 (ssh from c-jfischer3; `~/claude` is mounted)
- **Conda env:** `~/miniconda3/envs/twix` (Python 3.12.4)
- **Twix binary:** `~/miniconda3/envs/twix/bin/twix`
- **LP configs:** `~/work/twix/llm-configs/`
- **LLM server:** `http://sdeneefe-g7:8080` (Cohere-compatible)
- **Critical:** `LD_LIBRARY_PATH=/home/nbuild/pub/xmt/17.0.52/lib` — required or twix
  fails at startup (languageweaver XMT bindings load at import time even for decode)

## Available Cohere LP Configs

```
~/work/twix/llm-configs/
  SpaEng_COHERE_LLM_0_0_x_0
  EngEsl_COHERE_LLM_0_0_x_0
  EngSpa_COHERE_LLM_0_0_x_0
  EngFra_COHERE_LLM_0_0_x_0
  CO_COHERE-EXPERT-MT_LATEST       # language-agnostic expert MT model
```

## Command Template

```bash
LD_LIBRARY_PATH=/home/nbuild/pub/xmt/17.0.52/lib \
CO_API_URL=http://sdeneefe-g7:8080 \
CO_API_KEY='string-not-empty' \
~/miniconda3/envs/twix/bin/twix decode <input.txt> \
  --lp ~/work/twix/llm-configs/<LP_NAME> \
  --input-format txt \
  --attempt-type translation \
  --n-shot 0 \
  --report-every 1 \
  > output.jsonl 2> output.log
```

**Note:** `CO_API_URL` must be the base URL only — the Cohere client appends `/v2/chat`
automatically. Do NOT add `/chat` to the URL.

## Launching a Background Job

Always run from gpu-a6002, write outputs to `~/claude/work/<project>/`:

```bash
# Write a run script first, then:
ssh gpu-a6002 'nohup bash ~/claude/work/<project>/<run-script>.sh </dev/null &>/dev/null &'

# Check it launched:
ssh gpu-a6002 'ps aux | grep twix | grep -v grep'

# Monitor progress:
ssh gpu-a6002 'wc -l ~/claude/work/<project>/output-*.jsonl'
ssh gpu-a6002 'tail -5 ~/claude/work/<project>/output-*.log'
```

## Output Format

JSONL, one record per line:
```json
{"src_seg": "...", "tgt_seg": "...", "n_shot": 0, "constraints": null, "nmt_seg": null, ...}
```

## Smoke Test Before Full Run

Always test with a few lines before launching 74k+ line jobs:

```bash
ssh gpu-a6002 '
  head -3 <input.txt> | \
  LD_LIBRARY_PATH=/home/nbuild/pub/xmt/17.0.52/lib \
  CO_API_URL=http://sdeneefe-g7:8080 \
  CO_API_KEY=string-not-empty \
  ~/miniconda3/envs/twix/bin/twix decode - \
    --lp ~/work/twix/llm-configs/<LP_NAME> \
    --input-format txt --attempt-type translation \
    --n-shot 0 --report-every 1 2>&1
'
```

Expect to see `HTTP/1.1 200 OK` lines and JSON output within a few seconds.

## Common Errors and Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `Permission denied: python3.12` | Trying to use sdeneefe's uv venv | Use `~/miniconda3/envs/twix/bin/twix` instead |
| `libxmt_shared.so: undefined symbol` | Missing LD_LIBRARY_PATH | Add `LD_LIBRARY_PATH=/home/nbuild/pub/xmt/17.0.52/lib` |
| `404 page not found` on `/chat/v2/chat` | CO_API_URL has `/chat` appended | Use base URL only: `http://sdeneefe-g7:8080` |
| `XMT version does not meet minimum` | LP uses EuroLLM/TRTLLM backend, not Cohere | Switch to a `*_COHERE_LLM_*` LP config |
| `ModuleNotFoundError: lw_mt_metrics` | Wrong twix src in .pth file | Verify `~/miniconda3/envs/twix` points to correct src |

## Do NOT Use

- `/home/sdeneefe/work/twix/.venv/` — sdeneefe's venv, not executable by jfischer
- `SpaEng_Pro_LLM_3_0_x_0` or other `*_Pro_LLM_*` configs — these use EuroLLM via
  TRTLLM and require XMT 17.0.60 which isn't available to jfischer
- `CO_API_URL=http://sdeneefe-g7:8080/chat` — wrong, adds duplicate path segment

## Current Projects Using This Pattern

- `~/claude/work/poor-data-enrichment/` — SBCUSD SpaEng + EngEsl translation
  (74,250 lines each; outputs fed to MetricX for Poor-class mining)
  See also Cortex tag: `poor-data-enrichment`
