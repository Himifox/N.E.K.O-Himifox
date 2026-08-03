"""Local import sources used by the public knowledge runtime.

Remote maintenance adapters intentionally require direct module imports so the
normal application path cannot acquire Geng8 or Moegirl networking by accident.
"""

from .geng_guide import load_geng_guide_markdown

__all__ = [
    "load_geng_guide_markdown",
]
