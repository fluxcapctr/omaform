"""The adapter boundary.

An adapter turns one file format into `Document` plus `Blank` objects and writes
values back. It implements exactly two operations, so that adding DOCX in Phase 5
means writing one class and touching none of the matching, profile, template or
review code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model import Document


@runtime_checkable
class Adapter(Protocol):
    fmt: str
    extensions: tuple[str, ...]

    def discover(self, path: str) -> Document:
        """Find every blank in the file. Must not modify it."""
        ...

    def write(self, doc: Document, values: dict[str, str], out_path: str,
              images: dict[str, bytes] | None = None, *, lock_form: bool = False) -> None:
        """Write `values` (blank id to text) and `images` (blank id to PNG
        bytes, drawn over the blank) into a copy at `out_path`."""
        ...
