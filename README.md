# pi-loop-monitor

A terminal monitor for [pi](https://github.com/pi-ai/pi) multi-agent harness sessions. Watches tmux panes and file output for looping behaviour and automatically injects a stop prompt to break the agent out.

Built for use with local LLM inference stacks (llama.cpp / llama-swap) where agents can get stuck repeating themselves with no built-in circuit breaker.

![Python](https://img.shields.io/badge/python-3.8+-blue) ![Platform](https://img.shields.io/badge/platform-linux-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

---

## The problem

When running multi-agent pi sessions with local LLMs, agents sometimes loop — repeating the same plan, rewriting the same file, or spinning on the same task indefinitely. There's no built-in watchdog. You either catch it manually or burn context and tokens until the session dies.

## What it does

Two independent detection mechanisms, one tool:

**1. Output loop detection**
Polls every tmux pane in your pi session. On each poll it diffs the pane output against the previous capture to get only *new* lines, strips pi harness chrome (status bars, task lists, token counters), then hashes overlapping 4-line windows and scores them against a rolling history. If the same block of output recurs above the threshold, it injects a stop prompt directly into that pane via `tmux send-keys`.

**2. Duplicate file write detection**
Uses `inotifywait` to watch your projects directory in a background thread. When any file is saved, it hashes the content and compares against the previous save of that file. A duplicate write (same content saved twice) triggers a targeted stop prompt telling the agent exactly which file it duplicated.

Both triggers use separate, context-appropriate stop prompts and have independent cooldowns to avoid prompt spam.

---

## Requirements

- Python 3.8+
- tmux
- `inotify-tools` (for file watching)

```bash
sudo apt install inotify-tools
```

---

## Installation

```bash
# clone
git clone https://github.com/YOUR_USERNAME/pi-loop-monitor
cd pi-loop-monitor

# copy to your pi bin (optional)
cp pi_loop_monitor.py ~/.pi/bin/
chmod +x ~/.pi/bin/pi_loop_monitor.py
```

No dependencies beyond the Python standard library.

---

## Usage

```bash
# basic — watch session 1 (spawned agents)
python pi_loop_monitor.py --session 1

# watch both sessions
python pi_loop_monitor.py --session 1 --session2 0

# with file duplicate detection
python pi_loop_monitor.py --session 1 --watch-dir ~/.pi/projects

# dry run — detect but don't inject (use this to calibrate first)
python pi_loop_monitor.py --session 1 --dry-run
```

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--session` | `1` | Primary tmux session name to watch |
| `--session2` | — | Optional second session (e.g. orchestrator) |
| `--watch-dir` | `~/.pi/projects` | Directory to watch for duplicate file writes |
| `--interval` | `4` | Poll interval in seconds |
| `--threshold` | `4` | Repeat score before injecting stop prompt |
| `--cooldown` | `20` | Minimum seconds between injects per pane |
| `--min-new` | `3` | Minimum new lines required to run detection |
| `--dry-run` | off | Detect and display but do not inject |

---

## Calibrating the threshold

Run with `--dry-run` first and watch the `score=N` values in the TUI. Normal active agent work typically scores 0–1. A genuine output loop scores 4–8+ quickly. Set `--threshold` to sit between those two.

If you're getting false positives on panes with a lot of repetitive structure (e.g. large JSON outputs, repeated log lines), increase `--threshold` or `--min-new`.

---

## Stop prompts

**Output loop:**
```
STOP. You are looping. Do not repeat previous output.
Summarise where you are in one sentence, then ask what to do next.
```

**Duplicate file write:**
```
STOP. You just wrote the same file content twice: <path>.
Do not rewrite it. Check what you have already created and continue to the next task.
```

---

## TUI

```
────────────────── PI LOOP MONITOR ───────────────────
  14:23:01  │  sess=1 + sess=0  │  file watch: on  │  Ctrl-C to quit
──────────────────────────────────────────────────────
  ── session 1 ──────────────────────────────────────
  coder                    ● ok
  └─ Phase 3: writing quality-gate.md ↵ file created
  orchestrator             ● ok
  └─ Subagent completed. Moving to task #21

  ── session 0 ──────────────────────────────────────
  main                     ● ok
  └─ (no new output)

  ── duplicate file writes ───────────────────────────
  none detected

──────────────────────────────────────────────────────
  panes: 3  │  looping: 0  │  injects: 2  │  file writes: 14  dups: 0
```

States: `● ok` / `⚠ LOOP` / `⚡ INJECTED` / `⏸ cooldown` / `○ idle`

---

## How detection works

### Output loop detection

The key insight is that pi harness panes have a persistent TUI (task lists, status bars, token counters) that never changes. Naive hash-of-full-pane approaches false-positive constantly on this chrome.

The fix is **diff-based detection**:

1. Capture pane → strip ANSI → filter chrome lines via regex blocklist
2. Diff cleaned lines against previous capture → new lines only
3. Hash overlapping 4-line windows of new lines
4. Score against rolling hash history (last 60 windows)
5. Score ≥ threshold → loop detected

After injection, hash history is cleared so the monitor doesn't immediately re-trigger on the injected prompt itself.

### File duplicate detection

`inotifywait` watches the project directory recursively for `close_write` events. On each event the file content is MD5-hashed and compared to the stored hash from the previous write of that path. Files over 5MB are skipped.

---

## Tested with

- pi multi-agent harness
- llama-swap (port 12434)
- llama.cpp with Qwen3-27B, Gemma 4 27B
- Pop!_OS / Ubuntu 24, RTX 3090

---

## License

MIT
