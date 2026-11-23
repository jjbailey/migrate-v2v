#!/usr/bin/env python3
# bin/table_common.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Plain-text column table shared by the *-project-facts scripts."""

def format_table(rows, columns):
    """Header plus rows, columns left-aligned to their widest cell, as lines.

    Cells that are lists are comma-joined and None prints as empty.
    """
    cells = [[", ".join(map(str, v)) if isinstance(v := row.get(c), list)
              else ("" if v is None else str(v)) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells))
              for i, c in enumerate(columns)]
    return ["  " + "  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip()
            for line in [[c.upper() for c in columns], *cells]]
