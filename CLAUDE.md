# cc-dashboard

Live full-screen terminal dashboard for monitoring all running Claude Code sessions.

## Architecture

Single-file pure Python application (`src/cc_dashboard/__main__.py`, ~950 lines). Zero external dependencies — stdlib only. Linux-specific (reads `/proc` for process discovery).

### Key data flow

1. `get_running_claude_sessions()` — scans `ps` + `/proc` to find Claude processes
2. `assign_sessions_to_pids()` — matches processes to JSONL session files in `~/.claude/projects/`
3. `read_session_info()` — parses JSONL tail for status, messages, tools (with mtime cache)
4. `guess_status()` — heuristic status detection based on file age + entry types
5. `render_fullscreen()` — renders card-based TUI with Matrix rain animation

### Caching

- `_session_info_cache`: mtime-keyed cache for parsed session data (skips unchanged files)
- `_prompt_count_cache`: file-size-keyed incremental cache for user prompt counting

## Development

```bash
# Install in editable mode
pip install -e .

# Run from source
PYTHONPATH=src python -m cc_dashboard

# Run with custom interval
cc-dashboard -n 0.5
```

## Versioning

Version is tracked in two places — keep them in sync:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/cc_dashboard/__init__.py` → `__version__ = "X.Y.Z"`

## Publishing to PyPI

The package is published to Momenta's internal Artifactory PyPI registry.

```bash
# 1. Build the distribution
python -m build

# 2. Upload to Momenta internal PyPI
twine upload --repository local dist/*
```

Registry config is in `~/.pypirc` under the `[local]` section, pointing to:
`https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta`

Users install via:
```bash
pip install cc-dashboard -i https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta/simple
```

## Conventions

- Stdlib only — no external dependencies
- All logic in `__main__.py` — single-file architecture
- ANSI escape codes for terminal rendering (no curses)
- Default refresh interval: 1 second
