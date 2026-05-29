#!/usr/bin/env python3
"""
pi_loop_monitor.py — tmux session monitor for pi multi-agent harness
Watches pane output per agent AND file writes for duplicate detection.
Injects stop prompt on either trigger.

Usage:
    python3 pi_loop_monitor.py --session 1 [--session2 0] [options]

Options:
    --session   SESSION   primary tmux session (default: 1)
    --session2  SESSION   optional second session to watch
    --watch-dir DIR       directory to watch for duplicate file writes
                          (default: ~/.pi/projects)
    --interval  SECS      poll interval (default: 4)
    --threshold N         repeat score before inject (default: 4)
    --cooldown  SECS      min seconds between injects per pane (default: 20)
    --min-new   N         min new lines to run detection (default: 3)
    --dry-run             detect but don't inject
"""

import subprocess, time, argparse, re, hashlib, sys, os, signal, threading
from collections import deque, defaultdict
from datetime import datetime
from pathlib import Path

# ── ANSI ─────────────────────────────────────────────────────────────────────
R  = "\033[0m";  B   = "\033[1m";   DIM = "\033[2m"
RED= "\033[38;5;196m"; GRN= "\033[38;5;82m";  YLW= "\033[38;5;220m"
CYN= "\033[38;5;45m";  MGN= "\033[38;5;201m"; GRY= "\033[38;5;240m"
BLU= "\033[38;5;69m";  WHT= "\033[38;5;255m"; ORG= "\033[38;5;208m"
AGENT_COLOURS = [CYN, GRN, YLW, MGN, BLU, ORG, "\033[38;5;147m", "\033[38;5;120m"]

STOP_PROMPT = (
    "STOP. You are looping. Do not repeat previous output. "
    "Summarise where you are in one sentence, then ask what to do next."
)

STOP_PROMPT_FILE = (
    "STOP. You just wrote the same file content twice: {path}. "
    "Do not rewrite it. Check what you have already created and continue to the next task."
)

# Pi harness chrome to ignore in pane output
CHROME_PATTERNS = [
    r'^\s*[●○◆▸▹►]\s',
    r'panes watched', r'looping now', r'total injections',
    r'Ctrl-C to quit', r'watching tmux', r'PI LOOP MONITOR',
    r'^─+', r'YOLO\s+~/', r'^\s*\d+:\d+\s*$',
    r'orchestrator.*llama', r'coder.*llama', r'\d+%/\d+k',
    r'^\s*\d+ running', r'active\s+·\s+provider',
    r'Update Available', r'Package Updates', r'pi update',
    r'^\s*[-–]\s+pi-', r'@adamjenner', r'subagent.at.a.time',
    r'Below editor widget', r'Above editor widget',
    r'! Working\.\.\.', r'^\[coder\]', r'^\[orchestrator\]',
    r'tools.*denied',
]
CHROME_RE = re.compile('|'.join(CHROME_PATTERNS), re.IGNORECASE)

# ── helpers ──────────────────────────────────────────────────────────────────

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip()
    except Exception:
        return ""

def list_panes(session):
    out = run(["tmux", "list-panes", "-t", session, "-s",
               "-F", "#{window_index}:#{pane_index}:#{pane_title}:#{window_name}"])
    panes = []
    for line in out.splitlines():
        parts = line.split(":", 3)
        while len(parts) < 4:
            parts.append("")
        panes.append(tuple(parts[:4]))
    return panes

def capture_pane(session, window, pane, lines=120):
    target = f"{session}:{window}.{pane}"
    return run(["tmux", "capture-pane", "-t", target, "-p", "-J", f"-S-{lines}"])

def send_keys(session, window, pane, text):
    target = f"{session}:{window}.{pane}"
    run(["tmux", "send-keys", "-t", target, text, "Enter"])

def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;?]*[mGKHFJlh]', '', text)

def clean_lines(text):
    out = []
    for line in strip_ansi(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if CHROME_RE.search(s):
            continue
        out.append(s)
    return out

def diff_new_lines(prev_lines, curr_lines):
    if not prev_lines:
        return curr_lines
    prev_set = set(prev_lines)
    new = []
    for line in reversed(curr_lines):
        if line in prev_set:
            break
        new.append(line)
    new.reverse()
    if not new and curr_lines != prev_lines:
        new = curr_lines[-10:]
    return new

def rolling_repeat_score(new_lines, seen_hist, chunk=4):
    if len(new_lines) < chunk:
        return 0, None
    hashes = []
    for i in range(len(new_lines) - chunk + 1):
        block = "\n".join(new_lines[i:i+chunk])
        h = hashlib.md5(block.encode()).hexdigest()[:10]
        hashes.append(h)
    counts = defaultdict(int)
    for h in list(seen_hist) + hashes:
        counts[h] += 1
    if not counts:
        return 0, None
    worst = max(counts, key=counts.__getitem__)
    return counts[worst], worst

def file_hash(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None

# ── file watcher thread ───────────────────────────────────────────────────────

class FileWatcher(threading.Thread):
    """
    Uses inotifywait to watch a directory for close_write events.
    Compares file content hash against the previous write hash.
    Duplicate writes are added to self.events list for the main loop to pick up.
    """
    def __init__(self, watch_dir, dry_run=False):
        super().__init__(daemon=True)
        self.watch_dir  = watch_dir
        self.dry_run    = dry_run
        self.file_hashes = {}          # path -> last hash
        self.events      = []          # [(path, timestamp)] populated on duplicate
        self.total_dups  = 0
        self.total_writes= 0
        self._lock       = threading.Lock()
        self.running     = True
        self.available   = self._check_inotify()

    def _check_inotify(self):
        r = subprocess.run(["which", "inotifywait"], capture_output=True)
        return r.returncode == 0

    def run(self):
        if not self.available:
            return
        cmd = [
            "inotifywait", "-mr", self.watch_dir,
            "--format", "%w%f",
            "-e", "close_write",
            "--quiet"
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                if not self.running:
                    break
                path = line.strip()
                if not path:
                    continue
                # skip binary / large files
                try:
                    size = os.path.getsize(path)
                    if size > 5 * 1024 * 1024:
                        continue
                except Exception:
                    continue

                h = file_hash(path)
                if h is None:
                    continue

                with self._lock:
                    self.total_writes += 1
                    prev = self.file_hashes.get(path)
                    self.file_hashes[path] = h
                    if prev and prev == h:
                        self.total_dups += 1
                        self.events.append((path, now_str()))
        except Exception:
            pass

    def pop_events(self):
        with self._lock:
            evs = list(self.events)
            self.events.clear()
            return evs

    def stop(self):
        self.running = False

# ── TUI ──────────────────────────────────────────────────────────────────────

def clear():
    print("\033[H\033[J", end="")

def header(sessions, fw):
    w = os.get_terminal_size().columns
    title = " PI LOOP MONITOR "
    pad = (w - len(title)) // 2
    print(f"{B}{CYN}{'─'*pad}{title}{'─'*(w-pad-len(title))}{R}")
    sess_str = " + ".join(f"sess={s}" for s in sessions)
    fw_str = f"│  file watch: {GRN}on{R}" if fw.available else f"│  file watch: {YLW}no inotifywait{R}"
    print(f"{GRY}  {now_str()}  │  {sess_str}  {fw_str}  │  Ctrl-C to quit{R}")
    print(f"{GRY}{'─'*w}{R}")

def status_line(label, colour, state, preview_lines, inject_count, score=0):
    STATE_SYMBOLS = {
        "ok":       f"{GRN}●  ok{R}",
        "loop":     f"{RED}⚠  LOOP (score={score}){R}",
        "inject":   f"{YLW}⚡ INJECTED (output loop){R}",
        "file_dup": f"{ORG}⚡ INJECTED (duplicate file){R}",
        "no_data":  f"{GRY}○  idle{R}",
        "cooldown": f"{YLW}⏸  cooldown{R}",
    }
    sym = STATE_SYMBOLS.get(state, "?")
    inj_str = f" {DIM}[×{inject_count}]{R}" if inject_count else ""
    preview = " ↵ ".join(preview_lines[-3:])[-80:] if preview_lines else "(no new output)"
    print(f"  {colour}{B}{label:<24}{R} {sym}{inj_str}")
    print(f"  {GRY}{DIM}└─ {preview}{R}")

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Pi harness tmux loop + file duplicate monitor")
    ap.add_argument("--session",   default="1")
    ap.add_argument("--session2",  default=None)
    ap.add_argument("--watch-dir", default=os.path.expanduser("~/.pi/projects"))
    ap.add_argument("--interval",  type=float, default=4.0)
    ap.add_argument("--threshold", type=int,   default=4)
    ap.add_argument("--cooldown",  type=float, default=20.0)
    ap.add_argument("--min-new",   type=int,   default=3)
    ap.add_argument("--dry-run",   action="store_true")
    args = ap.parse_args()

    sessions = [args.session]
    if args.session2:
        sessions.append(args.session2)

    avail = run(["tmux", "list-sessions", "-F", "#{session_name}"]).splitlines()
    for s in sessions:
        if s not in avail:
            print(f"{RED}✗ tmux session '{s}' not found. Available: {avail}{R}")
            sys.exit(1)

    # ── per-pane state ────────────────────────────────────────────────────────
    pane_prev_lines  = defaultdict(list)
    pane_hash_hist   = defaultdict(lambda: deque(maxlen=60))
    pane_state       = defaultdict(lambda: "no_data")
    pane_new_preview = defaultdict(list)
    pane_injects     = defaultdict(int)
    pane_last_inj    = defaultdict(float)
    pane_score       = defaultdict(int)
    pane_label       = {}
    pane_colour      = {}
    colour_idx       = 0

    # ── recent file dup events for display ───────────────────────────────────
    recent_file_events = deque(maxlen=6)   # (path, time_str)
    file_inject_count  = 0

    # ── start file watcher ───────────────────────────────────────────────────
    fw = FileWatcher(args.watch_dir, dry_run=args.dry_run)
    if Path(args.watch_dir).exists():
        fw.start()
    else:
        fw.available = False

    print(f"{B}{CYN}Pi Loop Monitor starting…{R}")
    print(f"  sessions={sessions}  interval={args.interval}s  threshold={args.threshold}  cooldown={args.cooldown}s")
    print(f"  watch-dir={args.watch_dir}  inotifywait={'yes' if fw.available else 'NOT FOUND — install with: sudo apt install inotify-tools'}")
    if args.dry_run:
        print(f"  {YLW}DRY RUN — detection only, no injection{R}")

    # baseline capture
    for sess in sessions:
        for win, pane, title, wname in list_panes(sess):
            key = f"{sess}:{win}.{pane}"
            raw = capture_pane(sess, win, pane, 120)
            pane_prev_lines[key] = clean_lines(raw)
            pane_label[key]  = title or wname or key
            pane_colour[key] = AGENT_COLOURS[colour_idx % len(AGENT_COLOURS)]
            colour_idx += 1
    print(f"{GRY}Baseline captured. Monitoring…{R}")
    time.sleep(args.interval)

    def find_active_pane():
        """Best-guess: return (sess, win, pane) for the most active agent pane."""
        for sess in sessions:
            for win, pane, title, wname in list_panes(sess):
                label = title or wname or ""
                if any(x in label.lower() for x in ("coder","agent","worker","body")):
                    return sess, win, pane
        # fallback: first pane of first session
        for sess in sessions:
            panes = list_panes(sess)
            if panes:
                return sess, panes[0][0], panes[0][1]
        return None, None, None

    def poll():
        nonlocal colour_idx, file_inject_count

        # ── pane loop detection ───────────────────────────────────────────────
        for sess in sessions:
            for win, pane, title, wname in list_panes(sess):
                key = f"{sess}:{win}.{pane}"
                label = title or wname or key
                if key not in pane_label:
                    pane_label[key]  = label
                    pane_colour[key] = AGENT_COLOURS[colour_idx % len(AGENT_COLOURS)]
                    colour_idx += 1

                raw  = capture_pane(sess, win, pane, 120)
                curr = clean_lines(raw)
                if not curr:
                    pane_state[key] = "no_data"
                    continue

                new_lines = diff_new_lines(pane_prev_lines[key], curr)
                pane_prev_lines[key]  = curr
                pane_new_preview[key] = new_lines

                if len(new_lines) < args.min_new:
                    if pane_state[key] not in ("inject", "cooldown", "file_dup"):
                        pane_state[key] = "ok"
                    continue

                score, _ = rolling_repeat_score(new_lines, pane_hash_hist[key], chunk=4)
                pane_score[key] = score

                for i in range(max(0, len(new_lines) - 4 + 1)):
                    block = "\n".join(new_lines[i:i+4])
                    pane_hash_hist[key].append(hashlib.md5(block.encode()).hexdigest()[:10])

                now_t = time.time()
                if score >= args.threshold:
                    if now_t - pane_last_inj[key] > args.cooldown:
                        pane_state[key]    = "inject"
                        pane_last_inj[key] = now_t
                        pane_injects[key] += 1
                        pane_hash_hist[key].clear()
                        if not args.dry_run:
                            send_keys(sess, win, pane, STOP_PROMPT)
                    else:
                        pane_state[key] = "cooldown"
                else:
                    if pane_state[key] not in ("file_dup",):
                        pane_state[key] = "ok"

        # ── file duplicate detection ──────────────────────────────────────────
        for path, ts in fw.pop_events():
            recent_file_events.append((path, ts))
            file_inject_count += 1
            short = os.path.relpath(path, args.watch_dir)

            # find coder pane and inject
            sess, win, pane = find_active_pane()
            if sess:
                key = f"{sess}:{win}.{pane}"
                pane_state[key]    = "file_dup"
                pane_injects[key] += 1
                pane_last_inj[key] = time.time()
                if not args.dry_run:
                    msg = STOP_PROMPT_FILE.format(path=short)
                    send_keys(sess, win, pane, msg)

    def render():
        clear()
        header(sessions, fw)

        for sess in sessions:
            panes = list_panes(sess)
            if not panes:
                continue
            print(f"  {DIM}── session {sess} {'─'*30}{R}")
            for win, pane, title, wname in panes:
                key = f"{sess}:{win}.{pane}"
                status_line(
                    pane_label.get(key, key),
                    pane_colour.get(key, WHT),
                    pane_state[key],
                    pane_new_preview[key],
                    pane_injects[key],
                    pane_score[key],
                )

        # ── file events panel ─────────────────────────────────────────────────
        w = os.get_terminal_size().columns
        print(f"\n  {DIM}── duplicate file writes {'─'*20}{R}")
        if recent_file_events:
            for path, ts in list(recent_file_events)[-5:]:
                short = os.path.relpath(path, args.watch_dir)
                print(f"  {ORG}⚡{R} {ts}  {short}")
        else:
            print(f"  {GRY}{DIM}none detected{R}")

        # ── footer ────────────────────────────────────────────────────────────
        loops  = sum(1 for s in pane_state.values() if s in ("loop","inject","cooldown","file_dup"))
        total  = len(pane_state)
        injtot = sum(pane_injects.values())
        writes = fw.total_writes if fw.available else 0
        dups   = fw.total_dups   if fw.available else 0
        dr     = f"  {YLW}[DRY RUN]{R}" if args.dry_run else ""
        print(f"\n{GRY}{'─'*w}{R}")
        print(f"  {DIM}panes: {total}  │  looping: {loops}  │  injects: {injtot}  "
              f"│  file writes: {writes}  dups: {dups}{dr}{R}")

    signal.signal(signal.SIGINT, lambda *_: (fw.stop(), print(f"\n{GRY}Stopped.{R}"), sys.exit(0)))

    while True:
        try:
            poll()
            render()
        except Exception as e:
            print(f"\n{RED}Error: {e}{R}")
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
