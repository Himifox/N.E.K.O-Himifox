"""Safe defaults for the public Moegirl knowledge-base synchronizer."""

MOEGIRL_KNOWLEDGE_ENABLED = True
# CHIME is shipped as a fixed, MIT-licensed JSON asset in the application
# package. Enabling it never creates a CHIME network request.
CHIME_KNOWLEDGE_ENABLED = True
# Application-level kill switch for the two attributed encyclopedia sources.
# General web search is intentionally not implemented here: it is owned by
# the separately enabled ``web_search`` plugin.
MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_FALLBACK_ENABLED = True
# Wall-clock budget shared by serial Moegirl and Chinese Wikipedia requests.
# Public-meme web fallback has a separate two-second cap, keeping the total
# external I/O budget for one model-directed lookup near four seconds.
MOEGIRL_KNOWLEDGE_ENCYCLOPEDIA_TIMEOUT_SECONDS = 2.0
MOEGIRL_KNOWLEDGE_AUTO_CONTEXT_ENABLED = True
MOEGIRL_KNOWLEDGE_AUTO_CONTEXT_MAX_HITS = 1
# Source synchronization is scheduled immediately after a completed reply, never
# before the user receives that reply. It can be cancelled during shutdown.
MOEGIRL_KNOWLEDGE_SYNC_INTERVAL_SECONDS = 24 * 60 * 60
MOEGIRL_KNOWLEDGE_SYNC_MAX_ENTRIES = 20
MOEGIRL_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS = 12.0
MOEGIRL_KNOWLEDGE_REQUEST_DELAY_SECONDS = 0.35
MOEGIRL_KNOWLEDGE_SEED_QUERIES = (
    "不要停下来啊",
    "永远的神",
    "小丑竟是我自己",
    "有内鬼，终止交易",
    "我超，原",
    "yyds",
    "显眼包",
)
