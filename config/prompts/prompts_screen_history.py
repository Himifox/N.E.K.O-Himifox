# -*- coding: utf-8 -*-
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

"""Model-facing text for the screen-history request view.

``utils.screen_comment_guard.project_screen_history`` replaces the whole body
of a quarantined assistant message with this placeholder, in the request copy
only. The model reads it, the user never does. It says that the body is
missing rather than rephrasing it.

Every row must stay free of every marker form the guard detects (neither
the Chinese "screen" label nor the English one), so projecting a request view twice,
in any locale, is a no-op. ``tests/unit/test_screen_history_stopgap.py`` pins
that per row.

Resolved per request through ``prompts_sys._loc`` with the offline client's
``_tool_image_locale()``, the same locale the tool-image placeholder uses.
"""

SCREEN_HISTORY_PLACEHOLDER = {
    "zh": "[系统占位：此条历史回复正文未加载，原始记录保留在历史面板中，不能据此还原或声称完整复述。]",
    "zh-TW": "[系統佔位：此則歷史回覆正文未載入，原始紀錄保留在歷史面板中，不能據此還原或聲稱完整複述。]",
    "en": "[System placeholder: the body of this earlier reply was not loaded. The original record stays in the history panel; do not reconstruct it or claim to repeat it in full.]",
    "ja": "[システムのプレースホルダー：この過去の返信の本文は読み込まれていません。元の記録は履歴パネルに残っています。これを基に復元したり、完全に再現したと主張したりしないでください。]",
    "ko": "[시스템 자리표시자: 이 이전 답변의 본문은 불러오지 않았습니다. 원본 기록은 기록 패널에 남아 있으며, 이를 근거로 복원하거나 전부 그대로 말했다고 주장하지 마세요.]",
    "ru": "[Системная заглушка: текст этого прошлого ответа не загружен. Исходная запись сохранена в панели истории; не восстанавливайте его и не утверждайте, что повторяете его полностью.]",
    "es": "[Marcador del sistema: el cuerpo de esta respuesta anterior no se cargó. El registro original se conserva en el panel de historial; no lo reconstruyas ni afirmes repetirlo completo.]",
    "pt": "[Marcador do sistema: o corpo desta resposta anterior não foi carregado. O registro original permanece no painel de histórico; não o reconstrua nem afirme repeti-lo por completo.]",
}
