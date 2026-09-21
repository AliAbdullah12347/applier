"""Entry point for `python -m applier`.

The `applier` console script only exists after `pip install -e .`, and the
first thing a new checkout does is fail with "'applier' is not recognized".
This makes the package runnable straight out of the folder:

    python -m applier gui

Identical to the installed command in every respect — same Typer app, same
commands — it just does not need the install to have happened first.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
