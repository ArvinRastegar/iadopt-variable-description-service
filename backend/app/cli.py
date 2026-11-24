"""Operator commands, run in the backend container with the server's settings.

``python -m app.cli db status|migrate|backup|reclaim`` (``components/app-storage.md``, section 7),
``owner bind|rebind`` and ``legacy show|link|fold|purge-passwords``
(``components/accounts-roles.md``, sections 5 and 8). Exit codes: 0 on success, 1 on a refusal or
failure (with the error's name and message), 2 on a usage error.

Skeleton of the increment-1 tests gate: :func:`main` raises ``NotImplementedError`` until the
implementation gate.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one command and return its exit code.

    Args:
        argv: The arguments after ``python -m app.cli``; ``None`` for ``sys.argv[1:]``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError("The operator commands are specified but not implemented yet.")


if __name__ == "__main__":
    sys.exit(main())
