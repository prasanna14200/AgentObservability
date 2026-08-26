# Troubleshooting Log

## Phase 2

Issue: `python -m agentsentinel.data.synth_traces` failed with `ModuleNotFoundError`.

Cause: the project uses a `src/` package layout and had not been installed as a package.

Fix: set `$env:PYTHONPATH = "src"` before direct module execution.

Issue: tests failed with `ImportError: cannot import name 'UTC' from 'datetime'`.

Cause: the local `python` command resolves to Python 3.10, where `datetime.UTC` is unavailable.

Fix: use `datetime.timezone.utc`, which works on Python 3.10 and 3.11+.
