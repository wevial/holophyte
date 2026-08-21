# hollow2

A minimal software factory. One script, no frameworks.

- `factory.py <target-repo>` loops over unchecked tasks in the target's `TODO.md`.
- For each task: implementer (Claude Code, write access) → read-only reviewer →
  one fix round if needed → max 2 review rounds → auto-merge to `main` → check off.
- On any task that fails 2 rounds or produces no commits, the loop stops and
  leaves the branch behind for a human.

Usage: `python3 factory.py /srv/dev/hollow2test`
