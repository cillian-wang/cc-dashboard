#!/usr/bin/env python3
"""Live full-screen dashboard for all running Claude Code sessions."""

import argparse
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TODOS_DIR = CLAUDE_DIR / "todos"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"

# ── ANSI ─────────────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
INVERSE = "\033[7m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_SCREEN = "\033[2J\033[H"
MOVE_HOME = "\033[H"
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
ERASE_LINE = "\033[K"

FG_WHITE = "\033[97m"
FG_GRAY = "\033[90m"
FG_CYAN = "\033[36m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_RED = "\033[31m"
FG_MAGENTA = "\033[35m"
FG_BLUE = "\033[34m"
FG_BLACK = "\033[30m"

BG_CYAN = "\033[46m"
BG_GRAY = "\033[48;5;236m"
BG_DARK = "\033[48;5;233m"
BG_BAR = "\033[48;5;24m"
BG_YELLOW = "\033[43m"
BG_GREEN = "\033[42m"
BG_CARD_WAITING = "\033[43m"       # Bright yellow – NEEDS ATTENTION
BG_CARD_FINISHED = "\033[48;5;237m" # Subtle dark grey – done

FG_BRIGHT_GREEN = "\033[92m"
FG_BRIGHT_YELLOW = "\033[93m"
FG_DARK_GRAY = "\033[38;5;240m"


# ── Data helpers ─────────────────────────────────────────────────────────────

def cwd_to_project_dir(cwd):
    return re.sub(r'[^a-zA-Z0-9]', '-', cwd)


def _parse_etime_days(etime):
    """Parse ps elapsed time (e.g. '3-12:05:01', '05:01', '131-04:36:35') into days."""
    etime = etime.strip()
    if "-" in etime:
        return int(etime.split("-")[0])
    parts = etime.split(":")
    if len(parts) == 3:
        return int(parts[0]) / 24
    return 0


def get_running_claude_sessions():
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,lstart,etime,args", "--no-headers"],
            capture_output=True, text=True,
        )
    except Exception:
        return []
    sessions = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[0])
            tty = parts[1]
            lstart_str = " ".join(parts[2:7])
            etime = parts[7]
            cmd = " ".join(parts[8:])
        except (IndexError, ValueError):
            continue
        # Extract the executable (first token of the command)
        exe = cmd.split()[0] if cmd.split() else ""
        # Match "claude" exactly or any path ending in /claude
        # This catches CLI sessions (pts/), IDE plugins (Cursor, VSCode, Zed), etc.
        if not (exe == "claude" or exe.endswith("/claude")):
            continue
        cwd = ""
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        # Determine session source from tty, command path, and parent process
        if tty.startswith("pts/"):
            source = "terminal"
        elif "cursor-server" in cmd or "vscode-server" in cmd:
            source = "cursor"
        else:
            # Non-tty bare "claude" — check parent process to identify IDE
            source = "ide"
            try:
                with open(f"/proc/{pid}/status") as f:
                    for sline in f:
                        if sline.startswith("PPid:"):
                            ppid = int(sline.split()[1])
                            try:
                                pcmd = open(f"/proc/{ppid}/cmdline").read()
                                if "cursor" in pcmd.lower():
                                    source = "cursor"
                                elif "zed" in pcmd.lower() or "claude-code-acp" in pcmd:
                                    source = "zed"
                                elif "vscode" in pcmd.lower() or "code-server" in pcmd.lower():
                                    source = "vscode"
                            except OSError:
                                pass
                            break
            except OSError:
                pass
        # Filter out stale/dead sessions: IDE-spawned processes that lost their
        # connections have very few open file descriptors (< 30) and have been
        # running for over a day.  Terminal sessions on a pts/ are kept regardless.
        if source != "terminal":
            try:
                fd_count = len(os.listdir(f"/proc/{pid}/fd"))
                uptime_days = _parse_etime_days(etime)
                if fd_count < 30 and uptime_days >= 1:
                    continue
            except OSError:
                continue  # process vanished
        sessions.append({"pid": pid, "tty": tty, "start": lstart_str, "elapsed": etime, "cwd": cwd, "source": source})
    return sessions


def find_active_sessions_for_project(project_dir, count=5):
    proj_path = PROJECTS_DIR / project_dir
    if not proj_path.is_dir():
        return []
    jsonl_files = list(proj_path.glob("*.jsonl"))
    if not jsonl_files:
        return []
    jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return jsonl_files[:count]


def read_session_info(jsonl_path):
    session_id = jsonl_path.stem
    info = {
        "session_id": session_id, "slug": "", "last_user_msg": "",
        "last_assistant_msg": "", "permission_mode": "", "git_branch": "",
        "version": "", "mtime": jsonl_path.stat().st_mtime,
        "last_stop_reason": "", "last_tool_name": "",
        "has_tool_result_after": False, "has_progress_after": False,
    }
    try:
        # Count user prompts (fast grep over full file)
        try:
            cr = subprocess.run(
                ["grep", "-c", '"type":"user"', str(jsonl_path)],
                capture_output=True, text=True,
            )
            info["user_prompt_count"] = int(cr.stdout.strip()) if cr.returncode == 0 else 0
        except Exception:
            info["user_prompt_count"] = 0

        tail_n = "500" if jsonl_path.stat().st_size > 500_000 else "80"
        result = subprocess.run(["tail", "-" + tail_n, str(jsonl_path)], capture_output=True, text=True)

        last_stop = ""
        last_tool = ""
        saw_result = False
        saw_progress = False
        last_entry_type = ""  # type of the very last JSONL entry

        for line in result.stdout.strip().split("\n"):
            try:
                d = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if "slug" in d:
                info["slug"] = d["slug"]
            if "permissionMode" in d:
                info["permission_mode"] = d["permissionMode"]
            if "gitBranch" in d:
                info["git_branch"] = d["gitBranch"]
            if "version" in d:
                info["version"] = d.get("version", "")

            msg_type = d.get("type", "")
            if msg_type:
                last_entry_type = msg_type

            if msg_type == "assistant":
                stop = d.get("message", {}).get("stop_reason", "")
                if stop:
                    last_stop = stop
                    saw_result = False
                    saw_progress = False
                    if stop == "tool_use":
                        last_tool = ""
                        mc = d.get("message", {}).get("content", [])
                        if isinstance(mc, list):
                            for c in mc:
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    last_tool = c.get("name", "")
                # Extract assistant text
                content = d.get("message", {}).get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c["text"]
                            break
                if text:
                    info["last_assistant_msg"] = text

            elif msg_type == "user":
                content = d.get("message", {}).get("content", "")
                # Check for tool_result
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            saw_result = True
                # Extract user text
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c["text"]
                            break
                if text:
                    info["last_user_msg"] = text

            elif msg_type == "progress":
                saw_progress = True

        info["last_stop_reason"] = last_stop
        info["last_tool_name"] = last_tool
        info["has_tool_result_after"] = saw_result
        info["has_progress_after"] = saw_progress
        info["last_entry_type"] = last_entry_type
    except Exception:
        pass
    return info


def assign_sessions_to_pids(sessions):
    pids_by_project = {}
    for s in sessions:
        proj = cwd_to_project_dir(s["cwd"])
        pids_by_project.setdefault(proj, []).append(s)
    results = []
    for proj, pid_list in pids_by_project.items():
        recent_jsonls = find_active_sessions_for_project(proj, count=len(pid_list) + 2)
        session_infos = [read_session_info(j) for j in recent_jsonls]
        pid_list_sorted = sorted(pid_list, key=lambda p: p["pid"], reverse=True)
        for i, s in enumerate(pid_list_sorted):
            results.append((s, session_infos[i] if i < len(session_infos) else None))
    return results


def get_todos(session_id):
    todo_file = TODOS_DIR / f"{session_id}-agent-{session_id}.json"
    if not todo_file.exists():
        return []
    try:
        with open(todo_file) as f:
            todos = json.load(f)
        return todos if isinstance(todos, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def guess_status(session_info, todos, mtime):
    """Returns (status_name, badge_color, border_color, card_bg).

    card_bg is an ANSI BG code applied to the entire card, or "" for none.
    """
    if not session_info:
        return "unknown", FG_GRAY, FG_DARK_GRAY, ""

    age = time.time() - mtime
    stop = session_info.get("last_stop_reason", "")
    has_result = session_info.get("has_tool_result_after", False)
    last_type = session_info.get("last_entry_type", "")

    # ── 0. File actively being written → working (thinking, streaming, executing)
    if age < 5:
        return "working", FG_GREEN, FG_BRIGHT_GREEN, ""

    # ── 1. Last entry is user text or progress → Claude is thinking / tool running
    if last_type in ("user", "progress") and age < 60:
        return "working", FG_GREEN, FG_BRIGHT_GREEN, ""

    # ── 2. tool_use with no result → waiting for permission/input
    if stop == "tool_use" and not has_result:
        return "waiting", FG_WHITE, FG_BRIGHT_YELLOW, ""

    # ── 3. tool_use with result but stale → Claude may be generating
    if stop == "tool_use" and has_result:
        if age < 30:
            return "working", FG_GREEN, FG_BRIGHT_GREEN, ""
        return "finished", FG_WHITE, FG_DARK_GRAY, ""

    # ── 4. end_turn → Claude finished its response
    if stop == "end_turn":
        return "finished", FG_WHITE, FG_DARK_GRAY, ""

    # ── 5. Fallback
    if age > 120:
        return "finished", FG_WHITE, FG_DARK_GRAY, ""
    return "working", FG_GREEN, FG_BRIGHT_GREEN, ""


# ── Formatting helpers ───────────────────────────────────────────────────────

def visible_len(s):
    """Length of string excluding ANSI escape sequences, CJK chars counted as 2."""
    stripped = re.sub(r'\033\[[0-9;]*m', '', s)
    w = 0
    for c in stripped:
        eaw = unicodedata.east_asian_width(c)
        w += 2 if eaw in ('W', 'F') else 1
    return w


def truncate(s, max_len):
    s = s.replace("\n", " ").strip()
    w = 0
    for i, c in enumerate(s):
        cw = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
        if w + cw > max_len - 1:
            return s[:i] + "…"
        w += cw
    return s


def pad_right(s, width):
    """Pad a string (possibly with ANSI) to exact visible width."""
    vl = visible_len(s)
    if vl >= width:
        return s
    return s + " " * (width - vl)


def render_task_bar(todos):
    if not todos:
        return f"{FG_GRAY}no tasks{RESET}"
    total = len(todos)
    done = sum(1 for t in todos if t.get("status") == "completed")
    prog = sum(1 for t in todos if t.get("status") == "in_progress")
    bar_w = 16
    dw = round(done / total * bar_w) if total else 0
    pw = round(prog / total * bar_w) if total else 0
    rw = bar_w - dw - pw
    bar = f"{FG_GREEN}{'█' * dw}{RESET}{FG_YELLOW}{'█' * pw}{RESET}{FG_GRAY}{'░' * rw}{RESET}"
    return f"{bar} {done}/{total}"


def get_current_task(todos):
    for t in todos:
        if t.get("status") == "in_progress":
            return t.get("content", t.get("subject", ""))
    for t in todos:
        if t.get("status") == "pending":
            return f"(next) {t.get('content', t.get('subject', ''))}"
    return ""


def format_elapsed_human(etime):
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, rest = etime.split("-", 1)
        days = int(d)
        etime = rest
    parts = etime.split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = 0, int(parts[0]), int(parts[1])
    else:
        return etime
    if days > 0:
        return f"{days}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# ── Collect ──────────────────────────────────────────────────────────────────

def collect():
    sessions = get_running_claude_sessions()
    paired = assign_sessions_to_pids(sessions)
    rows = []
    for proc_info, session_info in paired:
        sid = session_info["session_id"] if session_info else ""
        mtime = session_info["mtime"] if session_info else 0
        todos = get_todos(sid) if sid else []
        st, sc, bc, cbg = guess_status(session_info, todos, mtime)
        rows.append({
            **proc_info,
            "status": st, "status_color": sc, "border_color": bc, "card_bg": cbg,
            "slug": (session_info or {}).get("slug", ""),
            "git_branch": (session_info or {}).get("git_branch", ""),
            "last_user_msg": (session_info or {}).get("last_user_msg", ""),
            "last_assistant_msg": (session_info or {}).get("last_assistant_msg", ""),
            "user_prompt_count": (session_info or {}).get("user_prompt_count", 0),
            "source": proc_info.get("source", "terminal"),
            "todos": todos,
            "task_bar": render_task_bar(todos),
            "current_task": get_current_task(todos),
        })
    return rows


# ── Full-screen render ───────────────────────────────────────────────────────

def _display_width(s):
    """Display width of a plain string (no ANSI), CJK = 2."""
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)


def wrap_text(text, width):
    """Word-wrap text to given width, return list of lines."""
    text = text.replace("\n", " ").strip()
    lines = []
    while text:
        if _display_width(text) <= width:
            lines.append(text)
            break
        # Walk char by char to find where to cut
        w = 0
        cut = 0
        last_space = -1
        for i, c in enumerate(text):
            cw = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
            if w + cw > width:
                cut = last_space if last_space > 0 else i
                break
            w += cw
            if c == " ":
                last_space = i
        else:
            cut = len(text)
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    return lines or [""]


def _render_cards(sessions_data, MW, H, interval):
    """Render session cards inside one outer border that spans full height."""
    buf = []
    BC = FG_DARK_GRAY  # outer border color

    box_w = MW - 2      # outer border width (1 char margin each side)
    content_w = box_w - 4  # inside │ + space each side

    def emit(line=""):
        buf.append(pad_right(line, MW))

    def outer_top(color=BC):
        return f" {color}┌{'─' * (box_w - 2)}┐{RESET}"

    def outer_bot(color=BC):
        return f" {color}└{'─' * (box_w - 2)}┘{RESET}"

    def outer_div(color=BC):
        return f" {color}├{'─' * (box_w - 2)}┤{RESET}"

    def rline(text="", bg="", bc=BC):
        """Content line inside the outer border."""
        inner = f" {pad_right(text, content_w)} "
        if bg:
            inner = inner.replace(RESET, RESET + bg)
            inner = f"{bg}{inner}{RESET}"
        return f" {bc}│{RESET}{inner}{bc}│{RESET}"

    total = len(sessions_data)

    # ── Top border (green if first card is working) ────────────────────
    first_working = sessions_data and sessions_data[0]["status"] == "working"
    emit(outer_top(FG_BRIGHT_GREEN if first_working else BC))

    if not sessions_data:
        # Fill interior with empty + centered message
        interior = H - 2  # minus top + bottom border
        for _ in range(interior // 2 - 1):
            emit(rline())
        msg = "No active Claude Code sessions found."
        emit(rline(f"{'':>{content_w // 2 - len(msg) // 2}}{FG_GRAY}{msg}{RESET}"))
        for _ in range(interior - interior // 2):
            emit(rline())
    else:
        # ── Card geometry ────────────────────────────────────────────
        # Interior lines = H - 2 (top/bottom border)
        # Each card uses: inner_h lines. Between cards: 1 divider line.
        # So: sum(inner_h) + (n-1) dividers = interior
        interior = H - 2
        n = total
        avail_for_cards = interior - (n - 1)  # subtract dividers
        avail_for_cards = max(avail_for_cards, n * 3)
        base_inner = avail_for_cards // n
        extra = avail_for_cards - base_inner * n
        card_inners = [base_inner + (1 if i < extra else 0) for i in range(n)]

        for idx, s in enumerate(sessions_data):
            inner_h = card_inners[idx]

            pid = s["pid"]
            tty = s["tty"]
            cwd = s["cwd"]
            elapsed = format_elapsed_human(s["elapsed"])
            st = s["status"]
            sc = s["status_color"]
            cbg = s.get("card_bg", "")
            slug = s.get("slug", "")
            branch = s.get("git_branch", "")
            last_msg = s.get("last_user_msg", "")
            last_reply = s.get("last_assistant_msg", "")

            short_cwd = cwd.replace(str(Path.home()), "~")
            icon = {"working": "*", "finished": "-", "waiting": "!", "unknown": "?"}.get(st, ".")
            badge = f"{sc}{BOLD}[{icon}] {st.upper()}{RESET}"

            card = []

            # Row 1: path + branch + elapsed ... status on the right
            r1_left = f"{FG_CYAN}{BOLD}{short_cwd}{RESET}"
            if branch:
                r1_left += f"  {FG_MAGENTA}@ {branch}{RESET}"
            r1_left += f"  {FG_GRAY}{elapsed}{RESET}"
            badge_plain = f"[{icon}] {st.upper()}"

            # Right strip for finished/waiting cards
            strip_w = 0
            strip_bg = ""
            if st == "finished":
                strip_w = len(badge_plain) + 2
                strip_bg = BG_CARD_FINISHED
            elif st == "waiting":
                strip_w = len(badge_plain) + 2
                strip_bg = "\033[48;5;208m"  # orange

            main_w = content_w - strip_w if strip_w else content_w

            if strip_w:
                r1 = r1_left  # badge goes in the strip
            else:
                gap = content_w - visible_len(r1_left) - len(badge_plain)
                if gap < 1:
                    gap = 1
                r1 = f"{r1_left}{' ' * gap}{badge}"
            card.append(r1)

            source = s.get("source", "terminal")
            meta = [f"pid:{pid}"]
            if source != "terminal":
                meta.append(source)
            else:
                meta.append(f"tty:{tty}")
            if slug:
                meta.append(f'"{slug}"')
            card.append(f"{FG_GRAY}{' · '.join(meta)}{RESET}")

            card.append(f"{FG_GRAY}{'╌' * main_w}{RESET}")

            if last_msg:
                card.append(f"{FG_BLUE}You:{RESET}    {DIM}{truncate(last_msg, main_w - 10)}{RESET}")

            if last_reply:
                reply_clean = last_reply.replace("\n", " ").strip()
                remaining_lines = inner_h - len(card)
                if remaining_lines >= 2:
                    wrap_w = main_w - 10
                    wrapped = wrap_text(reply_clean, wrap_w)
                    for j, wl in enumerate(wrapped[:remaining_lines]):
                        prefix = f"{FG_GREEN}Claude:{RESET} " if j == 0 else "         "
                        if j == remaining_lines - 1 and j < len(wrapped) - 1:
                            wl = wl[: wrap_w - 1] + "…"
                        card.append(f"{prefix}{DIM}{wl}{RESET}")
                elif remaining_lines >= 1:
                    card.append(f"{FG_GREEN}Claude:{RESET} {DIM}{truncate(reply_clean, main_w - 10)}{RESET}")

            # Build all inner_h lines (content + padding)
            all_lines = list(card[:inner_h])
            while len(all_lines) < inner_h:
                all_lines.append("")

            # Emit all lines
            line_bc = FG_BRIGHT_GREEN if st == "working" else BC
            for row, cl in enumerate(all_lines):
                if strip_w:
                    if st == "finished":
                        cl = cl.replace(RESET, RESET + DIM)
                        cl = f"{DIM}{cl}{RESET}"
                    left = pad_right(cl, main_w)
                    if row == 0:
                        b = badge.replace(RESET, RESET + strip_bg)
                        rtxt = f" {b} "
                        right = f"{strip_bg}{pad_right(rtxt, strip_w + 1)}{RESET}"
                    else:
                        right = f"{strip_bg}{' ' * (strip_w + 1)}{RESET}"
                    emit(f" {line_bc}│{RESET} {left}{right}{line_bc}│{RESET}")
                else:
                    emit(rline(cl, cbg, line_bc))

            # Divider / bottom border
            if idx < total - 1:
                # Divider: green if either adjacent card is working
                next_working = sessions_data[idx + 1]["status"] == "working"
                div_green = st == "working" or next_working
                emit(outer_div(FG_BRIGHT_GREEN if div_green else BC))

    # Fill remaining interior with empty lines
    while len(buf) < H - 1:
        emit(rline())

    # ── Bottom border (green if last card is working) ────────────────────
    last_working = sessions_data and sessions_data[-1]["status"] == "working"
    emit(outer_bot(FG_BRIGHT_GREEN if last_working else BC))

    return buf[:H]


def _render_sidebar(sessions_data, SW, H, interval):
    """Render the left sidebar into a list of H lines, each padded to SW width."""
    buf = []
    inner_w = SW - 3  # "│ " left + "│" right  (left wall + space + content + right wall)

    total = len(sessions_data)
    working = sum(1 for s in sessions_data if s["status"] == "working")
    waiting = sum(1 for s in sessions_data if s["status"] == "waiting")
    finished = sum(1 for s in sessions_data if s["status"] == "finished")

    def sline(text=""):
        """Sidebar content line: │ <text padded to inner_w> │"""
        return f"{FG_DARK_GRAY}│{RESET} {pad_right(text, inner_w)}{FG_DARK_GRAY}│{RESET}"

    def sdiv():
        return f"{FG_DARK_GRAY}├{'─' * (SW - 2)}┤{RESET}"

    # Top border
    buf.append(f"{FG_DARK_GRAY}┌{'─' * (SW - 2)}┐{RESET}")

    # Title
    title = "CLAUDE CODE"
    title_pad = (inner_w - len(title)) // 2
    buf.append(sline(f"{' ' * title_pad}{FG_WHITE}{BOLD}{title}{RESET}"))
    buf.append(sdiv())

    # Time
    now_str = datetime.now().strftime("%H:%M:%S")
    buf.append(sline(f" {FG_GRAY}{now_str}{RESET}"))
    buf.append(sline())

    # Session counts
    buf.append(sline(f" {FG_WHITE}{BOLD}{total}{RESET} {FG_GRAY}sessions{RESET}"))
    if working:
        buf.append(sline(f" {FG_BRIGHT_GREEN}* {working} working{RESET}"))
    if waiting:
        buf.append(sline(f" {FG_BRIGHT_YELLOW}! {waiting} waiting{RESET}"))
    if finished:
        buf.append(sline(f" {FG_DARK_GRAY}- {finished} done{RESET}"))

    buf.append(sdiv())

    # Stats table: project | time | prompts
    col_time = 8   # "1d 5h  "
    col_msgs = 4   # "269"
    col_name = inner_w - col_time - col_msgs - 1  # 1 for leading space
    hdr = f" {FG_GRAY}{pad_right('', col_name)}{pad_right('time', col_time)}{'msgs':>{col_msgs}}{RESET}"
    buf.append(sline(hdr))
    buf.append(sline(f" {FG_DARK_GRAY}{'─' * (inner_w - 1)}{RESET}"))

    source_icons = {"terminal": ">_", "cursor": "Cu", "vscode": "VS", "zed": "Ze", "ide": "ID"}
    for s in sessions_data:
        proj_name = os.path.basename(s["cwd"]) if s["cwd"] else "?"
        elapsed = format_elapsed_human(s["elapsed"])
        prompts = s.get("user_prompt_count", 0)
        st = s["status"]
        src_icon = source_icons.get(s.get("source", "terminal"), ">_")

        nc = FG_BRIGHT_GREEN if st == "working" else (FG_BRIGHT_YELLOW if st == "waiting" else FG_DARK_GRAY)
        name_t = truncate(proj_name, col_name - 3)
        row_str = f" {FG_GRAY}{src_icon}{RESET} {nc}{pad_right(name_t, col_name - 3)}{RESET}{FG_GRAY}{pad_right(elapsed, col_time)}{prompts:>{col_msgs}}{RESET}"
        buf.append(sline(row_str))

    buf.append(sdiv())

    # ── Matrix rain effect (only when something is working) ─────────────
    has_working = any(s["status"] == "working" for s in sessions_data)

    text_pool = ""
    if has_working:
        for s in sessions_data:
            reply = s.get("last_assistant_msg", "")
            if reply:
                text_pool += reply.replace("\n", " ") + " "
        if not text_pool:
            text_pool = "claude code >_ thinking..."
        text_pool = re.sub(r'[^\x20-\x7e]', '', text_pool)
        if not text_pool:
            text_pool = "claude"

    logo_art = [
        " \u2590\u259b\u2588\u2588\u2588\u259c\u258c",        #  ▐▛███▜▌
        "\u259d\u259c\u2588\u2588\u2588\u2588\u2588\u259b\u2598",  # ▝▜█████▛▘
        "  \u2598\u2598 \u259d\u259d",                     #   ▘▘ ▝▝
    ]
    logo_h = len(logo_art)
    logo_w = max(_display_width(l) for l in logo_art)
    lc = "\033[38;5;208m"  # orange, always

    # Slow horizontal drift for logo (changes every 0.5s, 5x slower than rain)
    logo_center = max((inner_w - logo_w) // 2, 0)
    logo_seed = int(time.time() * 2)  # changes every 0.5s
    random.seed(logo_seed)
    logo_dx = logo_center + random.randint(-2, 2)
    logo_dx = max(0, min(logo_dx, inner_w - logo_w))
    random.seed()  # restore true randomness

    if has_working:
        # ── Matrix rain + drifting logo ──────────────────────────────
        rain_start = len(buf)
        rain_end = H - 1 - logo_h  # reserve logo(3) + bottom border(1)
        rain_h = rain_end - rain_start

        if rain_h > 2:
            cols = inner_w
            t = time.time()
            rain_grid = [[' ' for _ in range(cols)] for _ in range(rain_h)]
            rain_color = [[0 for _ in range(cols)] for _ in range(rain_h)]

            for col in range(cols):
                seed = hash(f"matrix_{col}") & 0xFFFFFFFF
                speed = 0.8 + (seed % 100) / 80.0
                offset = (seed >> 8) % (rain_h + 6)
                trail_len = 3 + (seed >> 16) % 4

                head = int(t * speed + offset) % (rain_h + trail_len + 4)
                head_row = head - 4

                for row in range(rain_h):
                    dist = head_row - row
                    if dist == 0:
                        ci = int(t * speed * 3 + col * 7 + row) % len(text_pool)
                        rain_grid[row][col] = text_pool[ci]
                        rain_color[row][col] = 1
                    elif 0 < dist <= trail_len:
                        ci = int(t * speed * 2 + col * 13 + row * 3) % len(text_pool)
                        rain_grid[row][col] = text_pool[ci]
                        rain_color[row][col] = 2 if dist <= trail_len // 2 else 3

            GREEN_BRIGHT = "\033[92m"
            GREEN_MED = "\033[32m"
            GREEN_DIM = "\033[38;5;22m"

            for row in range(rain_h):
                line = ""
                for col in range(cols):
                    c = rain_color[row][col]
                    ch = rain_grid[row][col]
                    if c == 1:
                        line += f"{GREEN_BRIGHT}{BOLD}{ch}{RESET}"
                    elif c == 2:
                        line += f"{GREEN_MED}{ch}{RESET}"
                    elif c == 3:
                        line += f"{GREEN_DIM}{ch}{RESET}"
                    else:
                        line += " "
                buf.append(f"{FG_DARK_GRAY}│{RESET} {pad_right(line, inner_w)}{FG_DARK_GRAY}│{RESET}")
        else:
            while len(buf) < H - 1 - logo_h:
                buf.append(sline())

        # Drifting logo
        for ll in logo_art:
            buf.append(sline(f"{' ' * logo_dx}{lc}{ll}{RESET}"))
    else:
        # ── Idle: big "闲" + still Claude logo ───────────────────────
        xian_art = [
            "█▀▀  ▀▀█",
            "█ █  █ █",
            "█ ▀▀▀▀ █",
            "█  ██  █",
            "█ █▀▀█ █",
            "██▄██▄██",
        ]
        xian_h = len(xian_art)
        xian_w = max(len(l) for l in xian_art)
        total_art_h = xian_h + 1 + logo_h
        space_avail_idle = H - 1 - len(buf)

        if space_avail_idle >= total_art_h + 2:
            top_pad = (space_avail_idle - total_art_h) // 2
            for _ in range(top_pad):
                buf.append(sline())
            xian_dx = max((inner_w - xian_w) // 2, 0)
            for xl in xian_art:
                buf.append(sline(f"{' ' * xian_dx}{FG_DARK_GRAY}{xl}{RESET}"))
            buf.append(sline())
            logo_still_dx = max((inner_w - logo_w) // 2, 0)
            for ll in logo_art:
                buf.append(sline(f"{' ' * logo_still_dx}{lc}{ll}{RESET}"))
        else:
            while len(buf) < H - 1 - logo_h:
                buf.append(sline())
            logo_still_dx = max((inner_w - logo_w) // 2, 0)
            for ll in logo_art:
                buf.append(sline(f"{' ' * logo_still_dx}{lc}{ll}{RESET}"))

    # Fill remaining
    while len(buf) < H - 1:
        buf.append(sline())

    # Bottom border
    buf.append(f"{FG_DARK_GRAY}└{'─' * (SW - 2)}┘{RESET}")

    return buf[:H]


def render_fullscreen(sessions_data, interval, W, H):
    """Render a full-screen frame that fills W x H."""
    wide_mode = W > H

    if wide_mode:
        sidebar_w = max(W // 7, 18)  # narrow sidebar
        main_w = W - sidebar_w

        sidebar_buf = _render_sidebar(sessions_data, sidebar_w, H, interval)
        main_buf = _render_cards(sessions_data, main_w, H, interval)

        # Combine side by side
        lines = []
        for i in range(H):
            sl = sidebar_buf[i] if i < len(sidebar_buf) else pad_right("", sidebar_w)
            ml = main_buf[i] if i < len(main_buf) else pad_right("", main_w)
            lines.append(sl + ml)
        return "\n".join(lines)
    else:
        main_buf = _render_cards(sessions_data, W, H, interval)
        return "\n".join(main_buf)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Live full-screen dashboard for Claude Code sessions")
    parser.add_argument("-n", "--interval", type=float, default=3, help="Refresh interval in seconds (default: 3)")
    parser.add_argument("--once", action="store_true", help="Print once and exit (no live refresh)")
    args = parser.parse_args()

    if args.once:
        W = shutil.get_terminal_size().columns
        H = shutil.get_terminal_size().lines
        data = collect()
        print(render_fullscreen(data, args.interval, W, H))
        return

    # ── Live full-screen mode ────────────────────────────────────────────
    import select
    import termios
    import tty

    old_settings = None
    if sys.stdin.isatty():
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    def cleanup(*_):
        sys.stdout.write(ALT_SCREEN_OFF + SHOW_CURSOR)
        sys.stdout.flush()
        if old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGWINCH, lambda *_: None)  # handle resize gracefully

    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR_SCREEN)
    sys.stdout.flush()

    try:
        RENDER_HZ = 0.1  # re-render every 0.1s for smooth matrix rain
        data = None
        last_collect = 0

        while True:
            W = shutil.get_terminal_size().columns
            H = shutil.get_terminal_size().lines

            # Collect data every `interval` seconds
            now = time.time()
            if data is None or now - last_collect >= args.interval:
                data = collect()
                last_collect = now

            frame = render_fullscreen(data, args.interval, W, H)

            sys.stdout.write(MOVE_HOME + frame)
            sys.stdout.flush()

            # Sleep briefly, checking for keypress
            if sys.stdin.isatty():
                ready, _, _ = select.select([sys.stdin], [], [], RENDER_HZ)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q", "\x1b"):
                        cleanup()
            else:
                time.sleep(RENDER_HZ)
    finally:
        sys.stdout.write(ALT_SCREEN_OFF + SHOW_CURSOR)
        sys.stdout.flush()
        if old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
