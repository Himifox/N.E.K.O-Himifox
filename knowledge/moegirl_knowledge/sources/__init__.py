"""Local import sources used by the public knowledge runtime.

Remote maintenance adapters intentionally require direct module imports so the
normal application path cannot acquire Geng8 or Moegirl networking by accident.
"""

from .chime import (
    CHIME_COMMIT,
    CHIME_DATASET_URL,
    CHIME_LICENSE,
    ChimeDataset,
    load_bundled_chime_dataset,
)
from .geng_guide import load_geng_guide_markdown

__all__ = [
    "ChimeDataset",
    "CHIME_COMMIT",
    "CHIME_DATASET_URL",
    "CHIME_LICENSE",
    "load_bundled_chime_dataset",
    "load_geng_guide_markdown",
]
