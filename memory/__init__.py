# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Memory subsystem.

⚠️ LLM call conventions (project-level hard rules)
================================
**Any call in memory/ and utils/ going through ``utils.llm_client.create_chat_llm`` /
``ChatOpenAI``:**

1. **Do not pass ``temperature=...``**. Both default to ``None`` (not written into the
   request body), letting the model endpoint respond with its own default behavior. The
   same rule applies to any wrapper helper (e.g. ``FactStore._allm_call_with_retries``
   historically accepted ``temperature=``; it has been removed).
   Rationale: (1) compatibility with models that reject the parameter, such as
   o1/o3/gpt-5-thinking/Claude extended-thinking; (2) per-task custom temperatures
   (0.1/0.2/0.3/0.5/1.0) introduce hard-to-reproduce regressions.
   Gatekeeper: ``scripts/check_no_temperature.py`` (CI: ``.github/workflows/analyze.yml``).

2. **Models come from tiers; no hardcoded fallbacks**. Every LLM call goes through
   ``self._config_manager.get_model_api_config('summary'|'correction'|'emotion'|'vision'|...)``
   to fetch the ``api_config['model'] / ['base_url'] / ['api_key']`` triple. Do **not**
   write fallbacks like ``api_config.get('model', SETTING_PROPOSER_MODEL)`` — those are
   retired hardcodes (``SETTING_PROPOSER_MODEL`` / ``SETTING_VERIFIER_MODEL`` were
   decommissioned in 2026-04). If the tier isn't configured, ``api_config['model']`` is
   ``''`` and the request is explicitly rejected by the API; that is a configuration
   error which should surface directly, not be silently masked by a qwen-max fallback.

3. **Tiers used by memory submodules**: all active LLM paths run on the ``summary`` or
   ``correction`` tier (fact extraction / signal detection / reflection synthesis /
   fact dedup / recall rerank → ``summary``; recent.review +
   persona.correction + promotion merge → ``correction``). Do not introduce new
   hardcoded model names.

If you have a very specific reason to bypass this, delete
``scripts/check_no_temperature.py`` first and explain it in the PR description for the
reviewer to judge.
"""
import os
import shutil
import logging

from .recent import CompressedRecentHistoryManager
from .settings import ImportantSettingsManager
from .timeindex import TimeIndexedMemory
from .facts import FactStore
from .persona import PersonaManager
from .reflection import ReflectionEngine

_logger = logging.getLogger(__name__)


def _is_within_memory_root(memory_dir: str, name: str, character_dir: str) -> bool:
    """Whether character_dir is a DIRECT child of the memory root.

    A character name reaches this as a path component, and a historical
    unsafe one resolves somewhere else entirely: "." lands on the root, ".."
    escapes above it, and a name carrying a separator nests. Every sidecar
    store asks this before resolving a write, so the answer lives here
    rather than three times over.
    """
    # Before normalisation, because normalisation is what differs between
    # platforms: POSIX treats a backslash as an ordinary filename character,
    # so "a\b" arrives as a legal DIRECT child and every check below passes
    # it. On Windows the same name is a separator and gets rejected. The
    # backslash half is what makes the answer the same on both; the forward
    # slash is already refused below, because the basename can never equal a
    # name containing one -- it is listed here so the two read as one rule,
    # and so it still holds if that equality is ever relaxed. Measured: only
    # dropping the backslash half reddens the guard.
    if "/" in name or "\\" in name:
        return False
    # realpath, not abspath: abspath is pure string arithmetic and leaves a
    # symlink unresolved, so a memory/<name> pointing anywhere at all still
    # looked like a direct child and the sidecar was written THROUGH the link.
    # Both sides get the same treatment, so a memory root that is itself a
    # link (a tree moved to another drive) keeps working.
    root = os.path.realpath(str(memory_dir))
    resolved = os.path.realpath(character_dir)
    # DIRECT child, and named exactly for the character. "a/b" nests a level
    # deeper and leaves an "a/" behind that facts_sync reads as a character
    # of its own; "./x" lands on the same directory as a character actually
    # called "x" and would share its sidecar.
    return (
        os.path.dirname(resolved) == root
        and os.path.basename(resolved) == name
    )


def ensure_character_dir(memory_dir: str, name: str) -> str:
    """Return the character-specific directory memory_dir/{name}/, creating it if missing."""
    char_dir = os.path.join(str(memory_dir), name)
    os.makedirs(char_dir, exist_ok=True)
    return char_dir


# 旧文件名 → 新文件名的映射（不含 name 后缀）
#
# Borrowed from utils.character_memory rather than copied. The copy that used
# to live here had drifted three entries behind: time_indexed_{name}.db,
# facts_archive_{name}.json and reflections_archive_{name}.json were all
# renameable and selectable but never migrated, so a character whose only
# history was one of those files was offered in the panel and then reported
# as having none -- the startup migration left the file in the memory root
# while every reader looked inside memory/{name}/.
#
# utils.character_memory imports nothing from this package, so the direction
# is safe.
from utils.character_memory import (  # noqa: E402
    LEGACY_CHARACTER_MEMORY_FILE_MAP as _MIGRATION_MAP,
)


def migrate_to_character_dirs(memory_dir: str, names: list[str]) -> None:
    """One-time migration: move legacy memory_dir/{type}_{name}.ext into memory_dir/{name}/{type}.ext"""
    memory_dir = str(memory_dir)
    for name in names:
        char_dir = ensure_character_dir(memory_dir, name)
        for old_pattern, new_filename in _MIGRATION_MAP.items():
            old_filename = old_pattern.replace('{name}', name)
            old_path = os.path.join(memory_dir, old_filename)
            new_path = os.path.join(char_dir, new_filename)
            if os.path.exists(old_path) and not os.path.exists(new_path):
                try:
                    shutil.move(old_path, new_path)
                    _logger.info(f"[Memory] 迁移 {old_filename} → {name}/{new_filename}")
                except Exception as e:
                    _logger.warning(f"[Memory] 迁移失败 {old_filename}: {e}")
