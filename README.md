# cc-dashboard

Live full-screen terminal dashboard for all running Claude Code sessions.

## Install

```
pip install cc-dashboard
```

## Usage

```bash
# Live dashboard (refreshes every 1s)
cc-dashboard

# Single snapshot and exit
cc-dashboard --once

# Custom refresh interval
cc-dashboard -n 5
```

Press `q` or `Esc` to quit the live dashboard.

## Features

- Auto-discovers all running Claude Code sessions
- Shows session status (working / waiting / finished)
- Displays current task, last prompt, and Claude's reply
- Matrix rain animation while sessions are active
- Zero dependencies — stdlib only

## Requirements

- Python >= 3.8
- Linux (reads `/proc` for session discovery)
