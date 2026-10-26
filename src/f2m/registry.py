"""Parser base class and registry.

Adding a format = one module with a ``Parser`` subclass decorated with
``@register``. Outside packages can also register parsers through the
``f2m.parsers`` entry-point group.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import ClassVar

from f2m.context import ParseContext
from f2m.document import Document

log = logging.getLogger(__name__)


class Parser(ABC):
    #: Format ids this parser handles, as returned by ``f2m.detect.detect``.
    formats: ClassVar[tuple[str, ...]] = ()
    #: File extensions (lowercase, with dot) used as a hint by detection.
    extensions: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def parse(self, data: bytes, ctx: ParseContext) -> Document:
        """Turn file bytes into a Document."""


_parsers: dict[str, type[Parser]] = {}
_loaded = False


def register(cls: type[Parser]) -> type[Parser]:
    for fmt in cls.formats:
        if fmt in _parsers and _parsers[fmt] is not cls:
            log.info("parser for %r replaced by %s", fmt, cls.__name__)
        _parsers[fmt] = cls
    return cls


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    import f2m.parsers  # noqa: F401  (registers built-in parsers)

    for ep in entry_points(group="f2m.parsers"):
        try:
            obj = ep.load()
            if isinstance(obj, type) and issubclass(obj, Parser):
                register(obj)
        except Exception as e:  # a broken plugin must not break the pipeline
            log.warning("could not load parser plugin %s: %s", ep.name, e)


def get_parser(fmt: str) -> Parser | None:
    _load()
    cls = _parsers.get(fmt)
    return cls() if cls else None


def available() -> dict[str, type[Parser]]:
    _load()
    return dict(_parsers)


def extension_map() -> dict[str, str]:
    """Extension -> format id, from all registered parsers."""
    _load()
    out: dict[str, str] = {}
    for fmt, cls in _parsers.items():
        for ext in cls.extensions:
            out.setdefault(ext, fmt)
    return out
