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

"""Localized schema text for the user music-intent tool."""

from config.prompts._locale import normalize_prompt_locale


_MUSIC_INTENT_TOOL_TEXTS = {
    "zh": {
        "description": (
            "报告最新一条用户消息中要求现在播放、更换或停止音乐的意图。只有该消息本身构成当前操作请求时才调用；"
            "表达偏好或心情、讨论歌曲、提问或假设、请求推荐、转述他人、以及助手主动建议均不得调用。"
        ),
        "action": "要执行的音乐动作。",
        "target_type": "播放目标类型；停止时使用 generic。",
        "song": "用户要求播放的歌曲名。",
        "artist": "用户指定的歌手名。",
        "playlist": "用户指定的歌单名。",
        "query": "无法归入歌曲、歌手或歌单时使用的音乐搜索词。",
    },
    "zh-TW": {
        "description": (
            "回報最新一則使用者訊息中要求現在播放、更換或停止音樂的意圖。只有該訊息本身構成目前操作請求時才呼叫；"
            "表達偏好或心情、討論歌曲、提問或假設、請求推薦、轉述他人，以及助理主動建議時都不得呼叫。"
        ),
        "action": "要執行的音樂動作。",
        "target_type": "播放目標類型；停止時使用 generic。",
        "song": "使用者要求播放的歌曲名稱。",
        "artist": "使用者指定的歌手名稱。",
        "playlist": "使用者指定的播放清單名稱。",
        "query": "無法歸入歌曲、歌手或播放清單時使用的音樂搜尋詞。",
    },
    "en": {
        "description": (
            "Report an intent in the latest user message to play, change, or stop music now. Call only when that message "
            "itself requests a current action. Do not call for preferences or moods, song discussion, questions or "
            "hypotheticals, recommendation requests, quoted requests, or assistant-initiated suggestions."
        ),
        "action": "The music action to perform.",
        "target_type": "The playback target type; use generic when stopping.",
        "song": "The song title requested by the user.",
        "artist": "The artist requested by the user.",
        "playlist": "The playlist requested by the user.",
        "query": "A music search query when song, artist, and playlist do not apply.",
    },
    "ja": {
        "description": (
            "最新のユーザーメッセージに、今すぐ音楽を再生・変更・停止する依頼がある場合だけ報告します。"
            "好みや気分の表明、曲の話題、質問・仮定、推薦依頼、他人の発言の引用、アシスタント側からの提案では呼び出さないでください。"
        ),
        "action": "実行する音楽操作。",
        "target_type": "再生対象の種類。停止時は generic。",
        "song": "ユーザーが指定した曲名。",
        "artist": "ユーザーが指定したアーティスト名。",
        "playlist": "ユーザーが指定したプレイリスト名。",
        "query": "曲・アーティスト・プレイリスト以外の音楽検索語。",
    },
    "ko": {
        "description": (
            "최신 사용자 메시지가 지금 음악 재생, 변경 또는 중지를 요청할 때만 그 의도를 보고합니다. "
            "취향이나 기분 표현, 노래 토론, 질문이나 가정, 추천 요청, 다른 사람의 말 인용, 어시스턴트의 선제 제안에는 호출하지 마세요."
        ),
        "action": "실행할 음악 동작입니다.",
        "target_type": "재생 대상 유형이며 중지 시 generic을 사용합니다.",
        "song": "사용자가 요청한 곡명입니다.",
        "artist": "사용자가 지정한 아티스트입니다.",
        "playlist": "사용자가 지정한 재생목록입니다.",
        "query": "곡, 아티스트, 재생목록에 해당하지 않을 때의 음악 검색어입니다.",
    },
    "ru": {
        "description": (
            "Сообщайте о намерении воспроизвести, сменить или остановить музыку только тогда, когда последнее сообщение "
            "пользователя само содержит просьбу выполнить это сейчас. Не вызывайте инструмент для предпочтений, настроения, "
            "обсуждения песен, вопросов, предположений, просьб о рекомендации, цитат или инициативных предложений ассистента."
        ),
        "action": "Музыкальное действие.",
        "target_type": "Тип цели воспроизведения; для остановки используйте generic.",
        "song": "Название песни, указанное пользователем.",
        "artist": "Исполнитель, указанный пользователем.",
        "playlist": "Плейлист, указанный пользователем.",
        "query": "Поисковый запрос, если остальные типы не подходят.",
    },
    "es": {
        "description": (
            "Informa de la intención de reproducir, cambiar o detener música solo cuando el último mensaje del usuario pida "
            "esa acción ahora. No lo llames para preferencias, estados de ánimo, conversaciones sobre canciones, preguntas, "
            "hipótesis, solicitudes de recomendación, citas ni sugerencias iniciadas por el asistente."
        ),
        "action": "La acción musical que se debe realizar.",
        "target_type": "El tipo de objetivo; usa generic al detener.",
        "song": "El título solicitado por el usuario.",
        "artist": "El artista indicado por el usuario.",
        "playlist": "La lista indicada por el usuario.",
        "query": "Una búsqueda musical cuando no se aplique otro tipo.",
    },
    "pt": {
        "description": (
            "Informe a intenção de tocar, trocar ou parar música somente quando a mensagem mais recente do usuário pedir essa "
            "ação agora. Não chame para preferências, humor, conversa sobre músicas, perguntas, hipóteses, pedidos de recomendação, "
            "citações ou sugestões iniciadas pelo assistente."
        ),
        "action": "A ação musical a executar.",
        "target_type": "O tipo de alvo; use generic ao parar.",
        "song": "O título pedido pelo usuário.",
        "artist": "O artista indicado pelo usuário.",
        "playlist": "A playlist indicada pelo usuário.",
        "query": "Uma busca musical quando os outros tipos não se aplicarem.",
    },
}


def get_music_intent_tool_texts(language: str | None) -> dict[str, str]:
    locale = normalize_prompt_locale(
        language,
        default="en",
        simplified="zh",
        keep_traditional=True,
    )
    return _MUSIC_INTENT_TOOL_TEXTS.get(locale, _MUSIC_INTENT_TOOL_TEXTS["en"])
