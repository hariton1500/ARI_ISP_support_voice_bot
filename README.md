# Голосовой бот службы поддержки абонентов

Бот принимает звонки на номер **777** и ведёт свободный диалог на русском языке.
Доступны два явно переключаемых движка: основной **AITUNNEL STT → LLM → TTS**
и переключаемый режим **Yandex Realtime** с серверным VAD и LiveTTS. Цифровое меню
и DTMF-управление удалены. Баланс, состояние сети и завершение разговора
выполняются через общие голосовые инструменты.

Управление вызовом идёт через **Asterisk ARI** (WebSocket + REST), звук — через
**ExternalMedia + AudioSocket**. Asterisk сам подключается к боту по TCP и
передаёт PCM 8 кГц в обе стороны.

## Как это работает

Сторона Asterisk (`sip2.evpanet.com`, Asterisk 22.5) настроена на Stasis-приложение
`ivr_ai`. Менять диалплан не требуется.

```text
бот                                                    Asterisk
── WS /ari/events?app=ivr_ai ─────────────────────────▶ постоянное подключение
◀─ StasisStart {channel.id, caller.number} ─────────── звонок на 777
── POST /ari/channels/{caller}/answer ────────────────▶
── POST /ari/bridges?type=mixing ─────────────────────▶ bridgeId
── POST /ari/channels/externalMedia
     app=ivr_ai&external_host=<адрес>:9092
     &encapsulation=audiosocket&transport=tcp
     &format=slin&data=<uuid> ─────────────────────────▶ emId
◀─ TCP + первый фрейм UUID ──────────────────────────── привязка к вызову
── POST /bridges/{bridgeId}/addChannel ────────────────▶ caller + media
◀─ PCM абонента ─────────────────────────────────────── VAD → STT → LLM
── PCM ответа ────────────────────────────────────────▶ потоковый TTS
── DELETE channel/media/bridge ────────────────────────▶ очистка
```

Важные свойства:

- пока ARI WebSocket не подключён, вызов на 777 может сразу завершаться;
- `POST /channels/{id}/continue` не используется, поскольку выводит канал из Stasis;
- TCP связывается с вызовом по `data=<uuid>`; неизвестные UUID в ARI-режиме отклоняются;
- основной кодек — `slin`: PCM16, mono, little-endian, 8 кГц;
- AudioSocket использует 3-байтовый заголовок: тип + длина payload big-endian;
- обрабатываются UUID (`0x01`), завершение (`0x00`), аудио (`0x10`–`0x18`) и ошибка (`0xFF`).

## Два голосовых режима

| Режим | Включение | Обработка диалога |
|---|---|---|
| AITUNNEL | `VOICE_PROVIDER=aitunnel` (по умолчанию) | локальный VAD → STT → LLM → потоковый TTS 24 → 8 кГц |
| Yandex Realtime | `VOICE_PROVIDER=yandex_realtime` | постоянный WebSocket, серверный VAD, LLM и LiveTTS; PCM 8 → 16 → 8 кГц |

Для Realtime нужны `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` и те же markdown-промпты
`LLM_PROMPTS_DIR`. Модель по умолчанию — `speech-realtime-max-tts-live/latest`,
голос — `sofia`. Приветствие генерирует Realtime, старый `greeting.wav` не
проигрывается. При `VOICE_ECHO_GUARD=true` входной звук не передаётся в Realtime,
пока говорит бот, и ещё 500 мс после ответа; похожие на реплики бота расшифровки
отбрасываются. В этом режиме barge-in отключён. При `VOICE_ECHO_GUARD=false`
событие `speech_started` останавливает текущий ответ, но собственное эхо может
вызвать ложное перебивание.

### Переключение на том же номере, порту и Stasis

Оба режима используют существующую конфигурацию: номер 777, `ARI_APP=ivr_ai`,
`AUDIOSOCKET_PORT=9092`, `EXTERNAL_MEDIA_PORT=9092` и прежний
`EXTERNAL_MEDIA_HOST`. На Asterisk ничего менять не нужно.

**Перед переключением остановите текущий процесс бота.** Нельзя одновременно
запускать AITUNNEL и Yandex с одним портом и одним Stasis app: второй процесс не
сможет занять порт, а обработчики одного приложения будут конкурировать за вызовы.

Проверьте подключение и запустите Yandex с тем же `.env`:

```powershell
python tools/check_realtime.py
python -m app --check --voice-provider yandex_realtime
python -m app --voice-provider yandex_realtime
```

Либо запишите `VOICE_PROVIDER=yandex_realtime` в `.env` и используйте обычный
`python -m app`. Для возврата остановите Yandex и запустите
`python -m app --voice-provider aitunnel`.

При запуске WebSocket модель обязательно передаётся в query-параметре
`?model=gpt://<folder>/<model>`; без него Yandex возвращает `Query string should not be empty`.
Для локальной проверки без Asterisk используйте прежний порт:
`--no-ari --port 9092`, а в симуляторе — `--port 9092`.

В sidecar Realtime добавляются модель, длина системного промпта, фактические
секунды PCM 16 кГц на входе и выходе, а также `realtime_usage`: суммарные входные,
выходные, текстовые, аудио-, кешированные и инструментальные токены из всех
`response.done`. `uncached_input_tokens` вычисляется как входные минус кешированные;
поле `responses` показывает, сколько ответов действительно содержало usage.
`realtime_first_audio_latency_ms` сохраняет задержки от `speech_stopped` до первой
аудиодельты. Эти данные позволяют сопоставлять звонок с детализацией биллинга;
денежные тарифы в JSON не сохраняются. Телефонный тракт всё равно остаётся PCM
8 кГц: Realtime улучшает диалог и синтез, но не расширяет полосу канала.

## Возможности

- свободный голосовой диалог без озвученного меню;
- AITUNNEL: VAD, partial STT и SoXR VHQ 24 → 8 кГц;
- Yandex Realtime: серверный VAD, непрерывный ввод и LiveTTS 16 → 8 кГц;
- голосовое перебивание текущего ответа;
- общие функции: `get_billing_info`, `transfer_to_operator`, `end_call`;
- справочники тарифов и правил в system-промпте;
- запись входящего звука и JSON-метаданных вызова;
- предсказуемый fallback при тишине и ошибках провайдеров.

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# Заполните ARI_PASSWORD, EXTERNAL_MEDIA_HOST, AITUNNEL_API_KEY и биллинг.

python tools/make_placeholder_prompts.py
python -m app --ari-check
python -m app --billing-check 1 --billing-mock
python -m app --tts-check "Проверка синтеза речи"
python -m app --stt-check recordings/example.wav
python -m app --llm-check "Какие тарифы в Евпатории?"
python -m app --check
python -m app
```

`scenarios/default.json` сразу запускает свободный диалог. Совместимый вариант
`scenarios/llm-first.json` имеет ту же голосовую модель и другое имя сценария.

Полезные флаги: `--no-ari`, `--port`, `--voice-provider`, `--external-media-host`,
`--allow-unknown-sessions`, `--log-level DEBUG`, `--billing-check USER_ID`,
`--billing-mock`, `--tts-check TEXT`, `--stt-check AUDIO`, `--llm-check TEXT`,
`--tts-output PATH`, `--llm-prompts-dir PATH`, `--scenario PATH`.

## Сетевые требования

`EXTERNAL_MEDIA_HOST:EXTERNAL_MEDIA_PORT` должен быть доступен с Asterisk
`91.195.230.13`:

1. Пробросьте TCP-порт 9092 на машину бота, если используется NAT.
2. Разрешите входящие соединения только от доверенного Asterisk.
3. Если внешний порт отличается от локального, задайте `EXTERNAL_MEDIA_PORT` явно.
4. Открывать ARI-порт 8088 наружу со стороны бота не требуется: бот сам инициирует HTTP/WS.

Проверка доставки ExternalMedia (бот остановлен, порт свободен):

```powershell
python tools/check_external_media.py --host <внешний-адрес> --port 9092
```

## Диагностика

| Симптом | Что проверить |
|---|---|
| звонок сразу обрывается | ARI WebSocket, настройки `ARI_*`, запуск `python -m app --ari-check` |
| `externalMedia: 500` | доступность `EXTERNAL_MEDIA_HOST:PORT`, NAT и файрвол |
| нет звука | `format=slin`, подключение медиапотока, WAV/PCM и логи TTS |
| неизвестный UUID | соответствие `data` при ExternalMedia и первого AudioSocket-фрейма |
| нет ответов на речь | `AITUNNEL_API_KEY`, STT/LLM-логи, VAD и уровень входящего звука |
| бот слышит собственные ответы | оставьте `VOICE_ECHO_GUARD=true`; проверьте фильтр эха |
| биллинг недоступен | `BILLING_BASE_URL`, `BILLING_TOKEN`, HTTP-код и таймаут |

## Настройки (.env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ARI_URL` | — | ARI REST base URL |
| `ARI_USER` / `ARI_PASSWORD` / `ARI_APP` | — | учётные данные и Stasis app |
| `AUDIOSOCKET_HOST` | `0.0.0.0` | listen address |
| `AUDIOSOCKET_PORT` | `9092` | локальный AudioSocket-порт |
| `EXTERNAL_MEDIA_HOST` | автоматический IPv4 | адрес, доступный Asterisk; в production задать явно |
| `EXTERNAL_MEDIA_PORT` | `AUDIOSOCKET_PORT` | внешний порт |
| `EXTERNAL_MEDIA_FORMAT` | `slin` | PCM 8 кГц |
| `EXTERNAL_MEDIA_TRANSPORT` | `tcp` | транспорт |
| `EXTERNAL_MEDIA_ENCAPSULATION` | `audiosocket` | encapsulation |
| `EXTERNAL_MEDIA_TIMEOUT` | `5` | ожидание TCP, с |
| `ALLOW_UNKNOWN_SESSIONS` | `false` в `.env.example` | автономные соединения без ARI; в production `false` |
| `SCENARIO_PATH` | `scenarios/default.json` | сценарий живого диалога |
| `PROMPTS_DIR` | `prompts/placeholder` | WAV-промпты |
| `RECORDINGS_DIR` | `recordings` | аудио и метаданные |
| `RECORD_CALLS` | `true` | запись входящего звука |
| `INIT_TIMEOUT` | `10` | ожидание UUID, с |
| `IDLE_TIMEOUT` | `30` | отсутствие входящих фреймов, с |
| `MAX_CALL_SECONDS` | `600` | лимит вызова, с |
| `BILLING_BASE_URL` / `BILLING_TOKEN` | — | endpoint и Bearer token |
| `BILLING_TIMEOUT` | `10` | таймаут биллинга, с |
| `BILLING_MOCK` | `false` | тестовые данные без сети |
| `VOICE_PROVIDER` | `aitunnel` | `aitunnel` или `yandex_realtime` |
| `YANDEX_API_KEY` / `YANDEX_FOLDER_ID` | — | обязательны для Realtime |
| `YANDEX_REALTIME_URL` | `wss://ai.api.cloud.yandex.net/v1/realtime/openai` | WebSocket API |
| `YANDEX_REALTIME_MODEL` | `speech-realtime-max-tts-live/latest` | Realtime LiveTTS |
| `YANDEX_REALTIME_VOICE` | `sofia` | голос LiveTTS |
| `YANDEX_REALTIME_TIMEOUT` | `10` | подключение, с |
| `YANDEX_DATA_LOGGING` | `false` | логирование данных у Yandex |
| `AITUNNEL_API_KEY` | — | единый ключ STT/TTS/LLM |
| `AITUNNEL_BASE_URL` | `https://api.aitunnel.ru/v1` | OpenAI-совместимый API |
| `STT_MODEL` | `whisper-large-v3-turbo` | модель распознавания |
| `SPEECH_LANGUAGE` | `ru` | язык |
| `SPEECH_TIMEOUT` | `20` | таймаут STT/TTS, с |
| `TTS_MODEL` | `gpt-4o-mini-tts` | модель синтеза |
| `TTS_VOICE` | `nova` | голос |
| `TTS_FORMAT` | `pcm` | `pcm` или `wav` |
| `TTS_PCM_RATE` | `24000` | частота исходного PCM |
| `VOICE_INPUT` | `true` | VAD/STT |
| `VOICE_BARGE_IN` | `true` | голосовое прерывание |
| `VOICE_ECHO_GUARD` | `true` | защита от собственного эха |
| `PLAYBACK_LEAD_IN_MS` | `150` | тишина перед фразой |
| `VAD_START_MS` | `160` | начало речи |
| `VAD_SILENCE_MS` | `700` | конец реплики |
| `VAD_MIN_SPEECH_MS` | `200` | минимальная реплика |
| `VAD_RMS_THRESHOLD` | `300` | минимальный RMS |
| `VAD_NOISE_MULTIPLIER` | `2.5` | запас над шумом |
| `STT_PARTIAL_INTERVAL_MS` | `1200` | интервал partial STT; `0` отключает |
| `LLM_MODEL` | `gpt-4o-mini` | модель диалога |
| `LLM_PROMPTS_DIR` | `prompts/llm` | markdown-справочники |
| `LLM_TEMPERATURE` | `0.3` | температура |
| `LLM_MAX_TOKENS` | `300` | лимит ответа |
| `LLM_TIMEOUT` | `30` | таймаут LLM, с |
| `LLM_MAX_TOOL_ROUNDS` | `4` | раунды инструментов |
| `LLM_HISTORY_MESSAGES` | `40` | размер истории |
| `LOG_LEVEL` | `INFO` | логирование |

Пароль ARI, токен биллинга и ключи AITUNNEL/Yandex маскируются в логах.

## Сценарий живого диалога

Основной путь: `greeting` → `dialog` → свободная речь. Явная просьба оператора
может сработать как быстрый голосовой переход; остальные фразы передаются LLM.
После двух периодов тишины выполняется `no_input` → `fallback`.

```json
{
  "name": "default",
  "initial": "greeting",
  "steps": {
    "greeting": {"prompt": "greeting.wav", "next": "dialog"},
    "dialog": {
      "prompt": "dialog.wav",
      "text": "Чем я могу помочь?",
      "llm": true,
      "timeout_ms": 8000,
      "max_attempts": 2,
      "voice": {"оператор": "operator"},
      "on_timeout": "no_input"
    },
    "no_input": {"prompt": "no_input.wav", "next": "fallback"},
    "operator": {"prompt": "operator.wav", "hangup": true},
    "fallback": {"prompt": "fallback.wav", "hangup": true}
  }
}
```

Поля шага:

| Поле | Смысл |
|---|---|
| `prompt` | WAV относительно `PROMPTS_DIR` |
| `text` | TTS-текст, если WAV недоступен |
| `next` | следующий шаг без ожидания ввода |
| `voice` | карта «распознанная фраза → шаг» |
| `llm` | свободный диалог через LLM |
| `timeout_ms` | ожидание реплики |
| `max_attempts` | число периодов ожидания/нераспознанных команд |
| `on_timeout` / `on_invalid` | fallback-переходы |
| `hangup` | завершить после промпта |

Поле `dtmf` больше не поддерживается: старый сценарий с ним будет отклонён при
загрузке, а не проигнорирован без предупреждения.

## Речь: STT, VAD и TTS

```text
PCM 8 кГц
   └─► VAD (RMS + адаптивный шум)
         ├─ начало речи ─► voice barge-in, если разрешён
         ├─ partial interval ─► промежуточная STT-гипотеза
         └─ тишина ─► финальный STT ─► voice-команда или LLM
                                           └─► потоковый TTS ─► PCM 8 кГц
```

- STT: `POST /audio/transcriptions`, multipart WAV, `language=ru`.
- TTS: `POST /audio/speech`, обычно PCM 24 кГц.
- PCM обрабатывается состоятельным `soxr.ResampleStream(..., quality="VHQ")`;
  независимый ресемплинг отдельных чанков не используется.
- `AudioPlayer.play_stream()` отправляет 20-мс блоки по мере поступления.
- При `VOICE_ECHO_GUARD=true` входной звук не идёт в VAD, пока говорит бот;
  похожие на реплики бота STT-фразы отбрасываются. Голосовой barge-in в этом
  режиме фактически отключён. Для перебивания установите `false`, если нет эха.

Сравнение исходного и телефонного звука:

```powershell
python tools/save_tts_comparison.py "Здравствуйте! Проверка качества речи."
```

Сохраняются `original-24000.wav` и `current-8000.wav`.

## LLM и инструменты

В AITUNNEL LLM вызывается через `POST /chat/completions`; текст ответа получается
целиком, затем запускается потоковый TTS. В Yandex Realtime модель, распознавание
и синтез работают внутри одной сессии. В обоих режимах system-промпт собирается из:

- `support_evp.md` — роль и правила;
- `billing_account_data.md` — объяснение данных биллинга;
- `connection_terms.md` — условия подключения;
- `internet_private.md` — тарифы и адреса.

| Инструмент | Назначение |
|---|---|
| `get_billing_info(user_id)` | получить разрешённые поля абонента по названному голосом счёту |
| `transfer_to_operator()` | запросить перевод; пока реального телефонного перевода нет |
| `end_call()` | завершить вызов после прощания |

В Realtime function output возвращается через `conversation.item.create`, после
чего запрашивается следующий ответ. После успешного `get_billing_info` перевод
программно отклоняется, пока модель не озвучит результат; функция получает
`billing_answer_required` и запрашивается новый ответ. Перевод/завершение
выполняются только после текущего аудио. Номер счёта нормализуется и проверяется: только цифры, до 24
знаков. Модели не передаются `packet_id`, `access_hwid`, `snmp_*` и прочие
технические поля. История и лимит раундов AITUNNEL настраиваются через
`LLM_HISTORY_MESSAGES` и `LLM_MAX_TOOL_ROUNDS`; Realtime хранит контекст у себя.

## Биллинг

Запрос:

```text
POST {BILLING_BASE_URL}
Authorization: Bearer <BILLING_TOKEN>
Content-Type: application/json

{"userId": "<лицевой счёт>"}
```

В живом диалоге абонент называет счёт голосом, после чего LLM вызывает
`get_billing_info`. Поиск по номеру звонящего в основном сценарии не используется.
Ошибки сети, HTTP, таймаута и не-JSON превращаются в понятный ответ модели.

Проверка:

```powershell
python -m app --billing-check 1 --billing-mock
python -m app --billing-check 1
```

## Промпты

Поддерживаются WAV PCM 8/16/24/32 бит, mono/stereo и разные частоты, а также raw
`.sln`, `.sln8`, `.sln16`, `.pcm`, `.raw`. Всё приводится к PCM16 mono 8 кГц.
Путь не может выходить за `PROMPTS_DIR`.

`python tools/make_placeholder_prompts.py` создаёт пять тоновых файлов:
`greeting.wav`, `dialog.wav`, `no_input.wav`, `operator.wav`, `fallback.wav`.
Реальные записи размещайте в `prompts/ru` и задайте `PROMPTS_DIR=prompts/ru`.

## Записи и метаданные

При `RECORD_CALLS=true` создаются `<uuid>.wav` и `<uuid>.json`. WAV содержит
только входящий звук абонента. JSON включает сценарий, канал, caller, длительность,
шаги, расшифровки, partial-гипотезы, barge-in, объём TTS, LLM-ответы/инструменты,
причину завершения и последний результат биллинга. Цифровой ввод не записывается.

```json
{
  "session_id": "uuid",
  "channel_id": "call-1",
  "caller": "79001112233",
  "scenario": "default",
  "duration_seconds": 24.7,
  "steps": ["greeting", "dialog"],
  "transcripts": ["Какой у меня баланс?"],
  "llm_turns": 1,
  "llm_tool_calls": 1,
  "llm_replies": ["На вашем счёте 227 рублей 23 копейки."],
  "hangup_reason": "llm_end_call",
  "recording": "uuid.wav"
}
```

Записи и расшифровки содержат персональные данные: ограничьте доступ и задайте
срок хранения.

## Проверка без АТС

```powershell
python -m pytest
python -m app --no-ari --port 9092
python tools/simulate_call.py --input recordings/caller.wav --save recordings/reply.wav
```

Симулятор передаёт WAV реальным темпом, добавляет тишину для завершения реплики
и сохраняет ответ бота. Для полного диалога нужны настроенные STT/LLM/TTS.

## Структура проекта

```text
app/
  __main__.py             CLI и запуск
  config.py               настройки
  ari/                    REST, WebSocket, оркестрация
  audiosocket/            протокол, TCP, вызов, запись
  audio/                  PCM, SoXR, плеер, WAV
  realtime/               Yandex WebSocket и Realtime-сессия вызова
  speech/                 STT/TTS, VAD, сбор реплик
  dialog/                 сценарий, промпты, голосовые интенты
  llm/                    диалог, function calling, справочники
  billing/                клиент биллинга
scenarios/
  default.json            живой диалог по умолчанию
  llm-first.json          совместимый вариант живого диалога
prompts/llm/              system-промпт и знания
tools/
  make_placeholder_prompts.py
  simulate_call.py         голосовой симулятор АТС
  check_external_media.py
  check_realtime.py         проверка WebSocket и session.updated
  save_tts_comparison.py
tests/                    автоматические тесты
PROJECT_SPEC.md           полная спецификация для воссоздания
```

## Безопасность

- Не коммитьте `.env`, записи и секретные заметки.
- Используйте `ALLOW_UNKNOWN_SESSIONS=false` в ARI/production.
- Ограничьте AudioSocket по сети: UUID не заменяет аутентификацию.
- ARI HTTP/WS без TLS допустимы только в защищённой сети; предпочтительны HTTPS/WSS.
- STT/TTS/LLM отправляют речь и текст внешнему провайдеру.
- Не давайте LLM инструменты, изменяющие биллинг или инфраструктуру, без отдельной авторизации.
- До production добавьте проверку принадлежности счёта звонящему и минимизируйте billing/ПДн в JSON.

## Дальнейшие этапы

1. Реальный телефонный перевод оператору с обработкой занято/нет ответа.
2. Авторизация доступа к данным счёта.
3. Retention записей и строгая минимизация биллинга в метаданных.
4. Метрики задержек STT/LLM/TTS, jitter buffer и нагрузочное тестирование.
5. RAG при росте базы знаний; PostgreSQL и контейнерная тестовая АТС по потребности.
