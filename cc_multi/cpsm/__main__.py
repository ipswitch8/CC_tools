# -*- coding: utf-8 -*-
"""Entry point for `python -m cpsm` and the `cpsm` console script."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from cpsm.cli import dispatch

    return dispatch(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
