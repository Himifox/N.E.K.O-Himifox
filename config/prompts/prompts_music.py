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
            "当最新一条用户消息要求现在播放、更换或停止音乐，或它直接回答了助手刚刚提出的选歌问题时调用。"
            "表达偏好或心情、讨论歌曲、提问或假设、只请求推荐但未要求播放、转述他人的请求，以及未被用户接受的助手建议均不得调用；"
            "带引号的歌曲标题不属于转述。"
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
            "當最新一則使用者訊息要求現在播放、更換或停止音樂，或直接回答助理剛提出的選歌問題時呼叫。"
            "表達偏好或心情、討論歌曲、提問或假設、只要求推薦但未要求播放、轉述他人的請求，以及未被使用者接受的助理建議時都不得呼叫；"
            "加上引號的歌曲名稱不屬於轉述。"
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
            "Call when the latest user message asks to play, change, or stop music now, or directly answers the assistant's "
            "immediately preceding question about what to play. Do not call for preferences or moods, song discussion, "
            "questions or hypotheticals, recommendation-only requests, requests merely quoted or reported as someone else's "
            "words, or unaccepted assistant suggestions. A song title in quotation marks is not a quoted request."
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
            "最新のユーザーメッセージが今すぐ音楽の再生・変更・停止を依頼する場合、または直前の選曲質問に直接答える場合に呼び出します。"
            "好みや気分、曲の話題、質問・仮定、再生を伴わない推薦依頼、他人の発言として引用・伝聞された依頼、"
            "ユーザーが受け入れていない提案では呼び出さないでください。引用符付きの曲名は引用された依頼ではありません。"
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
            "최신 사용자 메시지가 지금 음악 재생, 변경 또는 중지를 요청하거나 직전의 선곡 질문에 직접 답할 때 호출합니다. "
            "취향이나 기분, 노래 토론, 질문이나 가정, 재생 없는 추천 요청, 다른 사람의 말로 인용되거나 전달된 요청, "
            "사용자가 수락하지 않은 제안에는 호출하지 마세요. 따옴표 안의 곡 제목은 인용된 요청이 아닙니다."
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
            "Вызывайте инструмент, когда последнее сообщение просит сейчас воспроизвести, сменить или остановить музыку либо "
            "прямо отвечает на предыдущий вопрос ассистента о выборе музыки. Не вызывайте его для предпочтений, обсуждения, "
            "вопросов, предположений, рекомендаций без воспроизведения, просьб, лишь процитированных или пересказанных от "
            "имени другого человека, либо не принятых пользователем предложений. Название песни в кавычках не является "
            "процитированной просьбой."
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
            "Llama cuando el último mensaje pida reproducir, cambiar o detener música ahora, o responda directamente a la "
            "pregunta inmediatamente anterior del asistente sobre qué reproducir. No llames para preferencias, conversaciones, "
            "preguntas, hipótesis, recomendaciones sin reproducción, peticiones meramente citadas o transmitidas como palabras "
            "de otra persona ni sugerencias no aceptadas por el usuario. Un título entre comillas no es una petición citada."
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
            "Chame quando a mensagem mais recente pedir para tocar, trocar ou parar música agora, ou responder diretamente à "
            "pergunta imediatamente anterior do assistente sobre o que tocar. Não chame para preferências, conversas, perguntas, "
            "hipóteses, recomendações sem reprodução, pedidos meramente citados ou relatados como palavras de outra pessoa nem "
            "sugestões não aceitas pelo usuário. Um título entre aspas não é um pedido citado."
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
