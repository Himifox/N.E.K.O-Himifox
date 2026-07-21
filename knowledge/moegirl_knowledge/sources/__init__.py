"""Remote source adapters for later synchronization phases."""

from .chime import (
    CHIME_COMMIT,
    CHIME_DATASET_URL,
    CHIME_LICENSE,
    ChimeDataset,
    load_bundled_chime_dataset,
)
from .chinese_wikipedia_api import ChineseWikipediaApiSource
from .moegirl_wiki_api import MoegirlWikiApiSource, SourceCandidate, SourcePage

__all__ = [
    "ChimeDataset",
    "CHIME_COMMIT",
    "CHIME_DATASET_URL",
    "CHIME_LICENSE",
    "ChineseWikipediaApiSource",
    "MoegirlWikiApiSource",
    "SourceCandidate",
    "SourcePage",
    "load_bundled_chime_dataset",
]
