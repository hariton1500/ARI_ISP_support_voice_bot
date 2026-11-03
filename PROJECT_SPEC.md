# PROJECT_SPEC — голосовой бот поддержки провайдера

> **Назначение:** дать достаточно информации для функционального воссоздания проекта с нуля без чтения исходного кода.
>
> **Статус:** текущая рабочая копия `ari-isp-support-voice-bot`, версия `0.4.0`. Актуализировано 02.11.2026: добавлен изолированный пилот Yandex Realtime; AITUNNEL остаётся режимом по умолчанию.
>
> **Важно:** документ не содержит пароли, API-ключи, токены и реальные записи абонентов. Для побайтовой копии нужен Git-репозиторий; здесь описаны требования, поведение и контракты.

---

## 1. Краткое описание

Сервис принимает телефонные звонки абонентов провайдера «Евпанет» и ведёт свободный голосовой диалог на русском языке.

Основные возможности:

- приём вызова через Asterisk ARI;
- двусторонний звук через ExternalMedia + AudioSocket;
- два явных движка: `VOICE_PROVIDER=aitunnel` (по умолчанию) или `yandex_realtime`;
- AITUNNEL: локальный VAD, STT, LLM и потоковый TTS 24 → 8 кГц;
- Yandex Realtime: один WebSocket для серверного VAD, LLM и LiveTTS, PCM 8 → 16 → 8 кГц;
- общие function calling-инструменты биллинга, перевода и завершения;
- запрос разрешённых данных абонента из биллинга по названному голосом счёту;
- эндпоинтинг по тишине, partial STT в AITUNNEL и голосовой barge-in;
- запись входящего звука и JSON-метаданных вызова;
- WAV-заглушки для приветствия, ошибок и завершения.

Цифровое меню и DTMF-управление удалены. Все содержательные запросы обрабатываются в живом диалоге. Явная просьба оператора может быть быстрым голосовым переходом; остальная речь передаётся LLM.

Основной сценарий — `scenarios/default.json`. `scenarios/llm-first.json` сохранён как совместимый вариант с тем же голосовым поведением и отдельным именем.

## 2. Цели и границы продукта

### 2.1. Цели

1. Отвечать на вопросы об интернете, тарифах, подключении и оплате.
2. Проводить первичную диагностику проблем со связью.
3. По названному абонентом лицевому счёту получать и озвучивать данные биллинга.
4. Передавать сложные вопросы живому оператору.
5. Завершать вызов по просьбе абонента или правилам сценария.
6. Сохранять диагностическую информацию для разбора качества и ошибок.

### 2.2. Границы

Бот не должен самостоятельно:

- менять тариф или настройки оборудования;
- списывать средства;
- подключать или отключать платные услуги;
- подтверждать платёж без подтверждения биллинга;
- принимать номер карты, CVV или коды из СМС;
- выполнять команды ОС по тексту абонента или LLM;
- выдумывать тарифы, состояние сети, сроки ремонта или подключения.

### 2.3. Известные незавершённые функции

Это ограничения текущей реализации, а не целевые требования:

1. Шаг `operator` проигрывает `operator.wav` и завершает вызов. Реального ARI-перевода на 508 пока нет.
2. Знание номера лицевого счёта является единственной проверкой перед запросом данных. Привязки счёта к подтверждённому звонящему или второго фактора нет.
3. В JSON вызова может сохраняться полный `raw`-ответ биллинга. Для production нужен строгий список разрешённых полей.
4. Нет RAG, PostgreSQL, docker-compose с тестовой АТС и production-метрик.
5. Нет полноценного jitter buffer для потокового TTS/Realtime.
6. Без ключа выбранного провайдера живой диалог недоступен: AITUNNEL требует `AITUNNEL_API_KEY`, Realtime — `YANDEX_API_KEY` и `YANDEX_FOLDER_ID`.
7. Реальное подключение Realtime проверено до `session.updated`, но живой телефонный вызов ещё не выполнен. Режим использует тот же номер 777, Stasis app `ivr_ai` и AudioSocket-порт 9092; перед переключением нужно остановить прежний процесс.

## 3. Технологический стек

- Python `>=3.11`;
- `asyncio` — сервер, задачи и таймауты;
- `aiohttp>=3.9,<4` — ARI, биллинг, AITUNNEL и Yandex Realtime WebSocket;
- `numpy>=1.26,<3` — PCM, VAD и обработка сигнала;
- `soxr>=1.0,<2` — качественный одноразовый и потоковый ресемплинг;
- `python-dotenv>=1.0,<2` — загрузка `.env`;
- `pytest` — dev-зависимость;
- Asterisk 22.5.2, ARI, ExternalMedia, AudioSocket;
- AITUNNEL OpenAI-совместимый API;
- Yandex AI Studio Realtime `speech-realtime-max-tts-live/latest`, LiveTTS `sofia`.

Новых Python-зависимостей для Realtime нет.

Проект рассчитан прежде всего на Windows/PowerShell; основная логика использует кроссплатформенный Python.

## 4. Архитектура

```text
Абонент
   │ телефонный вызов на 777
   ▼
Asterisk
   │ StasisStart и события каналов
   │ WebSocket + HTTP ARI
   ▼
AriEventStream ──► AriCallManager ──► PendingRegistry
                         │                  │
                         │ ExternalMedia    │ UUID → PendingCall
                         ▼                  ▼
                   AudioSocketServer
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
        CallSession             RealtimeCallSession
        (AITUNNEL)              (Yandex Realtime)
              │                        │
       Scenario + VAD           постоянный WebSocket
       STT → LLM → TTS          server VAD + LLM + LiveTTS
              │                        │
              └───────────┬────────────┘
                          ▼
                  VoiceTools + SoXR
                          │
                          ▼
                    BillingClient
```

### 4.1. Ответственность модулей

| Модуль | Ответственность |
|---|---|
| `app/__main__.py` | CLI, конфигурация, запуск и остановка |
| `app/config.py` | неизменяемые настройки и маскирование секретов |
| `app/ari/client.py` | REST ARI |
| `app/ari/events.py` | постоянный WebSocket и реконнект |
| `app/ari/manager.py` | жизненный цикл ARI-вызова |
| `app/audiosocket/protocol.py` | аудиофреймы, UUID, terminate и error |
| `app/audiosocket/registry.py` | связь UUID с ожидающим вызовом |
| `app/audiosocket/server.py` | TCP-сервер и явный выбор класса сессии по `VOICE_PROVIDER` |
| `app/audiosocket/session.py` | AITUNNEL-диалог, общие ARI/запись/завершение |
| `app/realtime/client.py` | WebSocket Yandex, session.update, PCM append и события |
| `app/realtime/session.py` | Realtime-диалог, playback, barge-in, функции и метрики |
| `app/audio/pcm.py` | WAV/PCM, SoXR, блоки и тишина |
| `app/audio/playback.py` | воспроизведение с темпом 20 мс и остановка |
| `app/audio/recorder.py` | инкрементальная запись WAV |
| `app/dialog/` | сценарии, промпты, голосовые интенты |
| `app/speech/` | STT/TTS, VAD и сбор реплик |
| `app/llm/` | LLM-клиент, история, инструменты и промпты |
| `app/billing/client.py` | контракт биллинга и нормализация ответа |

## 5. Жизненный цикл звонка через ARI

### 5.1. Интеграция

| Параметр | Значение |
|---|---|
| Asterisk | `sip2.evpanet.com`, версия 22.5.2 |
| ARI REST | `http://sip2.evpanet.com:8088/ari` |
| ARI WebSocket | `ws://sip2.evpanet.com:8088/ari/events?app=ivr_ai` |
| Stasis app | `ivr_ai` |
| Тестовый номер | `777` |
| Оператор по инструкциям | `508` |
| Транспорт ARI | HTTP/WS без TLS; доступ ограничивается сетью |

Пароль ARI хранится только в секретной конфигурации.

### 5.2. Последовательность

1. Поддерживать постоянный WebSocket `/ari/events?app=ivr_ai`.
2. Получить `StasisStart` с `channel.id` и `caller.number`.
3. Создать `PendingCall` со случайным UUID.
4. Выполнить `POST /channels/{caller}/answer`.
5. Создать mixing bridge: `POST /bridges?type=mixing`.
6. Создать ExternalMedia:

```text
POST /channels/externalMedia
  app=ivr_ai
  external_host=<доступный Asterisk host:port>
  encapsulation=audiosocket
  transport=tcp
  format=slin
  data=<uuid>
```

7. Дождаться TCP-подключения Asterisk к AudioSocket.
8. Первый значимый фрейм должен содержать тот же UUID.
9. Связать TCP с `PendingCall`, удалив его из pending registry.
10. Добавить caller и media channel в bridge:
    `POST /bridges/{bridge}/addChannel?channel=caller,media`.
11. Обрабатывать входящий PCM, вести голосовой диалог и отправлять PCM ответов.
12. Удалить media channel, bridge и caller channel через ARI.

### 5.3. События

Обрабатываются:

- `StasisStart`;
- `StasisEnd`;
- `ChannelHangupRequest`;
- `ChannelDestroyed`.

Для диагностики логируются также `ChannelStateChange`, `ChannelEnteredBridge`, `ChannelLeftBridge` и другие события. События телефонных кнопок не влияют на диалог.

### 5.4. WebSocket реконнект

- heartbeat: 30 секунд;
- экспоненциальная задержка переподключения: от 1 до 30 секунд;
- после успешного подключения задержка сбрасывается;
- ошибка одного события не должна разрывать поток.

Если WebSocket не подключён, Asterisk не имеет активного обработчика Stasis и вызов на 777 может сразу завершаться.

## 6. AudioSocket

### 6.1. Транспорт

- TCP-сервер запущен на стороне бота;
- Asterisk сам подключается к боту;
- заголовок фрейма: 3 байта — тип `1 байт`, длина payload `2 байта big-endian`;
- максимальный payload: `65535` байт.

### 6.2. Типы фреймов

| Тип | Назначение |
|---|---|
| `0x00` | завершение |
| `0x01` | UUID, ровно 16 бинарных байт |
| `0x10` | PCM16 mono LE, 8 кГц |
| `0x11`–`0x18` | PCM 12, 16, 24, 32, 44.1, 48, 96, 192 кГц |
| `0xFF` | ошибка Asterisk |

Прочие типы считаются неизвестными служебными фреймами, логируются на DEBUG и не участвуют в сценарии.

### 6.3. Привязка и отклонение соединений

1. До UUID разрешено ждать `INIT_TIMEOUT`.
2. Аудио и служебные фреймы до UUID пропускаются.
3. В ARI-режиме UUID должен находиться в `PendingRegistry`.
4. Неизвестный UUID отклоняется при `ALLOW_UNKNOWN_SESSIONS=false`.
5. В автономном режиме сессия может обслуживаться без `PendingCall`.
6. При завершении бот отправляет `0x00`, закрывает TCP и инициирует очистку ARI.

### 6.4. Таймауты

- `INIT_TIMEOUT` — ожидание UUID;
- `IDLE_TIMEOUT` — отсутствие входящих фреймов;
- `MAX_CALL_SECONDS` — максимальная длительность диалога;
- `EXTERNAL_MEDIA_TIMEOUT` — ожидание TCP после создания ExternalMedia.

## 7. Аудиотракт

### 7.1. Внутренний формат

```text
PCM signed 16-bit
little-endian
mono
8000 Hz
20 ms = 160 samples = 320 bytes
```

8 кГц ограничивает полосу частотой Найквиста 4 кГц. Это телефонное качество; исходное качество TTS 24 кГц после преобразования полностью не сохраняется.

### 7.2. Промпты

Поддерживаются:

- WAV PCM 8/16/24/32 бит;
- любое число каналов с приведением к mono;
- разные частоты дискретизации;
- raw Asterisk `.sln`, `.sln8`, `.sln16`, `.pcm`, `.raw`.

При загрузке промпт преобразуется в PCM16 mono 8 кГц. Путь не должен выходить за `PROMPTS_DIR`.

Если WAV отсутствует или повреждён:

1. использовать `text` шага через TTS;
2. если текста нет — продолжить шаг без звука и записать предупреждение.

### 7.3. Ресемплинг

Текущий стандарт — **SoXR VHQ**.

Для целого буфера использовать `soxr.resample(..., quality="VHQ")`.

Для TTS-потока использовать `soxr.ResampleStream`:

- входной PCM16 любой длины;
- состояние фильтра сохраняется между чанками;
- anti-alias-фильтр обязателен;
- нельзя ресемплить каждые 20 мс независимо;
- выход накапливается и нарезается на блоки 20 мс;
- `flush()` вызывает `resample_chunk(..., last=True)` и возвращает задержанный остаток;
- после `flush()` экземпляр не используется повторно.

Приёмочные свойства:

- длительность сохраняется с точностью до округления PCM;
- результат потока совпадает с цельным SoXR с точностью до округления PCM16;
- тон 7 кГц при преобразовании 24 → 8 кГц подавляется более чем на 40 дБ;
- все полные выходные блоки имеют по 320 байт при 8 кГц.

### 7.4. Воспроизведение

- каждый блок отправляется с реальным темпом;
- перед фразой добавляется `PLAYBACK_LEAD_IN_MS` тишины;
- голосовой barge-in возможен только при выключенном echo guard;
- при отставании плеер не отправляет накопленную пачку одним burst;
- `stop()` досрочно завершает текущий промпт/TTS.

## 8. Сценарный движок

### 8.1. Формат сценария

```json
{
  "name": "default",
  "description": "Свободный голосовой диалог",
  "initial": "greeting",
  "steps": {
    "greeting": {
      "label": "приветствие",
      "prompt": "greeting.wav",
      "next": "dialog"
    }
  }
}
```

### 8.2. Поля шага

| Поле | Тип и назначение |
|---|---|
| `label` | человекочитаемое имя |
| `prompt` | аудиофайл относительно `PROMPTS_DIR` |
| `text` | TTS-текст, если WAV недоступен |
| `next` | следующий шаг без ожидания ввода |
| `voice` | объект `фраза → шаг` |
| `llm` | свободный диалог через LLM |
| `timeout_ms` | ожидание реплики, по умолчанию 8000 |
| `max_attempts` | попытки, по умолчанию 2, минимум 1 |
| `on_invalid` | переход после нераспознанной голосовой команды |
| `on_timeout` | переход после тишины |
| `hangup` | завершить после шага |
| `billing` | расширение движка: `caller` или конкретный userId; в основном сценарии не используется |
| `prompt_not_found` | WAV, если абонент не найден |
| `prompt_error` | WAV, если биллинг недоступен |

При загрузке проверяются типы, неотрицательный timeout, число попыток и существование переходов. Поле `dtmf` явно отклоняется с ошибкой, чтобы старое меню не было проигнорировано без предупреждения.

### 8.3. Обычный голосовой шаг

1. При наличии `billing` выполнить запрос и выбрать prompt.
2. Проиграть WAV или TTS.
3. Если `hangup=true`, завершить.
4. Если нет `voice`, перейти по `next`.
5. Иначе ждать распознанную фразу `timeout_ms`.
6. Совпадение → перейти по карте `voice`.
7. Незнакомая фраза или тишина → повторить шаг.
8. После исчерпания попыток использовать `on_invalid`/`on_timeout`; при отсутствии перехода завершить диалог.

### 8.4. Голосовые команды

Нормализация:

- lowercase;
- `ё` → `е`;
- знаки препинания заменяются пробелами;
- повторные пробелы удаляются.

Фразы из `voice` ищутся в распознанном тексте; более длинные имеют приоритет. Числительные не преобразуются в команды меню.

### 8.5. `default.json` и `llm-first.json`

Оба файла описывают один и тот же путь:

| Шаг | Поведение |
|---|---|
| `greeting` | greeting.wav → dialog |
| `dialog` | dialog.wav или «Чем я могу помочь?», затем свободный LLM-диалог |
| `no_input` | no_input.wav → fallback |
| `operator` | operator.wav → hangup |
| `fallback` | fallback.wav → hangup |

Параметры `dialog`: ожидание 8 с, 2 попытки, голосовые переходы «оператор» и «соедините с оператором», `on_invalid=no_input`, `on_timeout=no_input`.

Баланс запрашивается через LLM-инструмент по названному счёту, а не через caller. Статических шагов меню, оплаты, техподдержки и баланса в основном сценарии нет.

## 9. STT, VAD и входная речь

### 9.1. STT API

```text
POST {AITUNNEL_BASE_URL}/audio/transcriptions
Authorization: Bearer <AITUNNEL_API_KEY>
Content-Type: multipart/form-data
```

Поля:

- `file`: WAV PCM16 mono;
- `model`: `STT_MODEL`;
- `response_format`: `json`;
- `language`: `SPEECH_LANGUAGE`, по умолчанию `ru`.

Ожидаемый ответ:

```json
{"text": "распознанная фраза"}
```

Ошибки HTTP, сети, таймаута, не-JSON и нестрокового `text` преобразуются в `SpeechError` и не должны ронять вызов.

### 9.2. VAD

Алгоритм — RMS по 20-мс кадрам с адаптивной шумовой дорожкой.

| Параметр | По умолчанию |
|---|---:|
| начало речи | 160 мс громких кадров подряд |
| конец реплики | 700 мс тишины |
| минимальная реплика | 200 мс |
| максимальная реплика внутри VAD | 15 000 мс |
| минимальный RMS | 300 |
| множитель шума | 2.5 |

Порог:

```text
max(VAD_RMS_THRESHOLD, noise_floor * VAD_NOISE_MULTIPLIER)
```

Шумовая дорожка обновляется только на тихих кадрах. В начало реплики добавляется pre-roll, чтобы не потерять первые звуки.

### 9.3. Промежуточные гипотезы

- во время речи каждые `STT_PARTIAL_INTERVAL_MS` можно отправлять накопленный PCM в STT;
- `0` отключает partial STT;
- одновременно выполняется не более одной partial-задачи на сессию;
- при финальной реплике partial-задача отменяется;
- partial-результаты используются для диагностики, финальный текст участвует в диалоге;
- в JSON сохраняются последние 20 partial-гипотез.

### 9.4. Echo guard и barge-in

При `VOICE_ECHO_GUARD=true`:

- входной звук не подаётся в VAD/STT, пока бот говорит;
- VAD сбрасывается до и после воспроизведения;
- STT-текст, похожий на недавнюю фразу бота, отбрасывается;
- окно проверки — 6 секунд;
- минимальная нормализованная длина — 8 символов без пробелов;
- порог похожести `SequenceMatcher` — 0.72;
- голосовой barge-in во время речи бота фактически отключён.

При `VOICE_ECHO_GUARD=false` и `VOICE_BARGE_IN=true` начало речи останавливает WAV/TTS.

## 10. TTS

### 10.1. API

```text
POST {AITUNNEL_BASE_URL}/audio/speech
Authorization: Bearer <AITUNNEL_API_KEY>
Content-Type: application/json
```

Запрос:

```json
{
  "model": "gpt-4o-mini-tts",
  "input": "Текст ответа",
  "voice": "nova",
  "response_format": "pcm"
}
```

Поддерживаются `response_format=pcm` и `wav`.

### 10.2. Потоковый режим

1. Получать HTTP-ответ чанками по 4096 байт.
2. Для PCM использовать `PcmStreamResampler(TTS_PCM_RATE, 8000)` на SoXR.
3. Отправлять готовые 20-мс блоки в AudioPlayer по мере поступления.
4. После конца HTTP-потока вызвать `flush()`.
5. Для WAV прочитать ответ, проверить PCM16 mono и ресемплить при необходимости.

Ошибка TTS логируется и увеличивает `speech_errors`, но не должна неожиданно ронять сервер.

## 11. LLM и function calling

### 11.1. API

```text
POST {AITUNNEL_BASE_URL}/chat/completions
Authorization: Bearer <AITUNNEL_API_KEY>
Content-Type: application/json
```

Параметры запроса:

- `model=LLM_MODEL`;
- `messages`: system + история;
- `temperature=LLM_TEMPERATURE`;
- `max_tokens=LLM_MAX_TOKENS`;
- `tools` и `tool_choice=auto`, если инструменты доступны.

LLM-текст не стримится: сначала получается полный `message`, затем запускается TTS.

### 11.2. System prompt

Формируется из:

1. `support_evp.md` — роль и правила;
2. `billing_account_data.md` — трактовка биллинга;
3. `connection_terms.md` — условия подключения;
4. `internet_private.md` — тарифы и адреса;
5. `TOOL_GUIDE` — правила вызова инструментов.

Все доступные справочники передаются целиком в system-сообщение при каждом запросе.

Основные правила модели:

- отвечать по-русски, кратко, 1–2 предложения;
- задавать не более одного вопроса;
- не повторять приветствие;
- не выдумывать данные;
- перед биллингом попросить номер счёта;
- при неопределённости предложить оператора;
- перед `end_call` сначала попрощаться;
- не показывать абоненту JSON и технические поля.

### 11.3. История

- отдельный conversation на каждый LLM-шаг вызова;
- максимум `LLM_HISTORY_MESSAGES` сообщений;
- обрезка не должна начинаться посередине связки assistant/tool;
- максимум `LLM_MAX_TOOL_ROUNDS` раундов на реплику;
- минимальное фактическое число раундов — 1;
- в JSON сохраняются последние 20 LLM-ответов.

### 11.4. Инструменты

#### `get_billing_info(user_id)`

- вызывается после того, как абонент назвал счёт;
- удаляет пробелы и символы `-() +`;
- принимает только цифры, максимум 24 знака;
- возвращает разрешённые поля абонента;
- ошибки превращаются в `invalid_user_id`, `billing_not_configured` или `billing_unavailable`.

#### `transfer_to_operator()`

- устанавливает `control.transfer=true`;
- возвращает номер 508;
- AITUNNEL переходит на шаг `operator`, если он существует;
- Realtime завершает вызов после текущего сообщения с причиной `realtime_transfer`;
- текущее ограничение: реального телефонного перевода оба перехода не выполняют.

#### `end_call()`

- устанавливает `control.end_call=true`;
- после озвучки прощания завершает диалог.

Неизвестный инструмент возвращает `unknown_tool` и не выполняет побочных действий.

### 11.5. Обработка ответа

Перед TTS удаляются fenced code, markdown-заголовки, ссылки с сохранением текста, маркеры списков, символы выделения и лишние пробелы.

Если текст пуст:

- end call → «Всего доброго.»;
- transfer → «Перевожу вас на оператора.»;
- иначе → просьба повторить.

При ошибке AITUNNEL LLM бот говорит о технической ошибке и переходит к оператору или fallback сценария.

### 11.6. Yandex Realtime

Это отдельный путь, а не реализация старых `SpeechBackend`/`LLMBackend`: локальные VAD, STT, LLM и TTS не создаются. На каждый вызов создаются `RealtimeCallSession` и собственный WebSocket. Наследование от `CallSession` сохраняет AudioSocket, запись WAV, sidecar, max-call timeout и ARI-очистку.

Подключение:

```text
wss://ai.api.cloud.yandex.net/v1/realtime/openai?model=gpt://<YANDEX_FOLDER_ID>/speech-realtime-max-tts-live/latest
Authorization: Api-Key <YANDEX_API_KEY>
x-data-logging-enabled: false
model URI: gpt://<YANDEX_FOLDER_ID>/speech-realtime-max-tts-live/latest
```

`YANDEX_DATA_LOGGING=true` меняет последний заголовок на `true`; по умолчанию логирование содержимого у провайдера выключено. После подключения отправляется `session.update`:

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt://<folder>/speech-realtime-max-tts-live/latest",
    "instructions": "<собранный LLMPromptSet.system>",
    "output_modalities": ["audio"],
    "audio": {
      "input": {
        "format": {"type": "audio/pcm", "rate": 16000},
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.5,
          "silence_duration_ms": 700
        }
      },
      "output": {
        "format": {"type": "audio/pcm", "rate": 16000},
        "voice": "sofia"
      }
    },
    "tools": [
      {
        "type": "function",
        "name": "get_billing_info",
        "description": "...",
        "parameters": {"type": "object"}
      }
    ]
  }
}
```

Все три схемы `VoiceTools` преобразуются из вложенного OpenAI-формата `{type, function: {...}}` в плоский `{type, name, description, parameters}`.

Входной тракт:

1. `_on_audio` продолжает писать исходный PCM 8 кГц в WAV и передаёт его `_feed_voice`.
2. Отдельная async-задача использует один `PcmStreamResampler(8000, 16000)` на весь звонок.
3. Каждый готовый блок отправляется как `input_audio_buffer.append` с base64 PCM16 mono 16 кГц.
4. При `server_vad` событие `input_audio_buffer.commit` не отправляется.
5. При `VOICE_ECHO_GUARD=true` входной звук не передаётся во время речи бота и 500 мс после неё; похожие на недавние реплики бота расшифровки отбрасываются. Голосовой barge-in в этом режиме отключён. При `false` вход идёт непрерывно и `speech_started` может перебить ответ, включая ложное срабатывание от эха.

После настройки `response.create` просит модель кратко поприветствовать от имени поддержки «Евпанет» и спросить, чем помочь. WAV-приветствие сценария не используется.

Обрабатываемые события:

| Событие | Действие |
|---|---|
| `input_audio_buffer.speech_started` | при выключенном echo guard остановить AudioPlayer, очистить аудиоочередь, отбросить поздние дельты старого response |
| `input_audio_buffer.speech_stopped` | сохранить monotonic timestamp для задержки |
| `conversation.item.input_audio_transcription.completed` | отбросить похожее на реплику бота эхо либо добавить transcript и увеличить `llm_turns` |
| `response.created` | создать очередь, SoXR 16 → 8 кГц и playback task |
| `response.output_audio.delta` | декодировать base64, потоково ресемплить и сразу ставить в playback |
| `response.output_audio.done` | вызвать `flush()` у resampler текущего response |
| `response.output_audio_transcript.done` | сохранить текст ответа и число символов |
| `response.output_item.done` с `function_call` | запомнить вызов до `response.done` |
| `response.done` | накопить `response.usage`, завершить очередь аудио и запустить финализатор |
| `error` | залогировать, увеличить `llm_errors`, корректно завершить вызов |

Playback task один на response, добавляет `PLAYBACK_LEAD_IN_MS` и использует тот же paced `AudioPlayer`. Для `function_call` финализатор сначала ждёт окончания/перебивания текущего аудио, затем вызывает общий `VoiceTools.execute`, отправляет:

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "<call_id>",
    "output": "<JSON-строка>"
  }
}
```

Для биллинга затем отправляется `{"type":"response.create"}`. После успешного `get_billing_info` устанавливается ожидание голосового ответа: если следующий function-only ответ вызывает `transfer_to_operator`, выполнение перевода блокируется, функция получает `billing_answer_required`, затем запрашивается новый ответ. После `response.output_audio_transcript.done` ограничение снимается. При разрешённых `end_call`/`transfer_to_operator` новый ответ не запрашивается: вызов завершается после уже полученного прощания/сообщения с причиной `realtime_end_call`/`realtime_transfer`. Реального перевода на 508 по-прежнему нет.

При shutdown отменяются input/event/finalizer/playback tasks, закрываются WebSocket и HTTP-сессия, затем выполняется общая очистка `CallSession`. Обрыв AudioSocket или `abort()` пробуждает Realtime-диалог и не оставляет ожидание WebSocket навсегда.

Переключение движка не меняет телефонную интеграцию: оба режима используют номер 777, `ARI_APP=ivr_ai`, `AUDIOSOCKET_PORT=9092`, `EXTERNAL_MEDIA_PORT=9092` и тот же `EXTERNAL_MEDIA_HOST`. Для Yandex остановить процесс AITUNNEL и запустить `python -m app --voice-provider yandex_realtime` с тем же `.env`; для возврата использовать `--voice-provider aitunnel`. Одновременный запуск двух процессов с одним портом/Stasis app не допускается. Отдельный номер, приложение Asterisk и порт не требуются.

## 12. Биллинг

### 12.1. Запрос

```text
POST {BILLING_BASE_URL}
Authorization: Bearer <BILLING_TOKEN>
Content-Type: application/json
Accept: application/json

{"userId": "<лицевой счёт>"}
```

В основном сценарии счёт берётся только из речи абонента и передаётся через LLM-инструмент. Поиск по caller в живом диалоге не используется.

### 12.2. Ответ

Найден:

```json
{
  "found": true,
  "user": {
    "id": "1",
    "extra_account": 227.23,
    "packet_name": "Интернет 100 Мбит",
    "packet_sum": 200,
    "packet_secs": 1065600,
    "days_left": 12,
    "network_status": {
      "switch_state": "green",
      "is_in_network_failure_zone": false,
      "reason": "Текстовое пояснение"
    },
    "active_repair": false
  }
}
```

Не найден:

```json
{"found": false, "user": null}
```

HTTP >=400, сетевые ошибки, таймаут и не-JSON считаются `BillingError`.

### 12.3. Нормализованная модель

| Поле | Источник |
|---|---|
| `user_id` | `user.id` |
| `balance` | `extra_account` |
| `packet_name` | `packet_name` |
| `packet_sum` | `packet_sum` |
| `packet_seconds` | `packet_secs` |
| `days_left` | `days_left` |
| `network_state` | `network_status.switch_state` |
| `network_reason` | `network_status.reason` |
| `in_failure_zone` | `network_status.is_in_network_failure_zone` |
| `active_repair` | `active_repair` |

Числа могут приходить строками; десятичная запятая заменяется точкой. `BILLING_MOCK=1` возвращает тестовые данные без сети.

### 12.4. Фильтр для LLM

Разрешены только:

- `id`;
- `extra_account`;
- `packet_name`;
- `packet_sum`;
- `days_left`;
- `auto_activation_enabled`;
- `network_status.switch_state`;
- `network_status.is_in_network_failure_zone`;
- `network_status.reason`;
- `active_repair`, `active_repair_comment`, `active_repair_date`.

Не передавать модели `packet_id`, `access_hwid`, `snmp_*` и другие технические поля.

## 13. Знания для ответов

Эти данные должны быть перенесены в prompts или эквивалентную базу знаний. Перед реальным использованием их необходимо проверить у владельца продукта.

### 13.1. Новое подключение

- обязательна единовременная оплата первых 12 месяцев до подключения;
- нельзя подключить с оплатой только одного месяца;
- сумма равна 12 месячным платежам выбранного тарифа;
- после оплаченного периода можно платить помесячно;
- не обещать рассрочку, скидку, возврат или исключения;
- для оформления передавать оператору.

### 13.2. Общие тарифные правила

- пакет действует 30 суток;
- скорость указывается как «до … Мбит», не гарантируется;
- цена зависит от населённого пункта, адреса и категории жилья;
- сначала уточнить населённый пункт и адрес;
- не перечислять всю базу сразу, предложить 2–4 подходящих варианта;
- база тарифов помечена датой 24.07.2025 и требует подтверждения актуальности.

### 13.3. Тарифные линейки

| Линейка | Категория | Тарифы, ₽/мес | Подключение |
|---|---|---|---|
| A | квартиры в многоэтажках; в Евпатории также 2–3-этажные дома | 15 Мбит — 150; 100 — 200; 200 — 400; 300 — 500; 500 — 600 | бесплатно |
| B | частный сектор и квартиры в общих дворах | 50 — 250; 100 — 300; 200 — 400; 300 — 500; 500 — 700; 1000 — 900 | бесплатно |
| C | Молочное | 50 — 250; 100 — 300; 200 — 400; 500 — 700; 1000 — 900 | бесплатно |
| D | Мирный | 30 — 200; 100 — 350; 200 — 800; 300 — 1000 | бесплатно |
| E | частный сектор Штормовое, Поповка | 50 — 300; 100 — 350; 200 — 400; 300 — 500; 500 — 700; 1000 — 900 | бесплатно |
| F | Витино, Ветеран, Крыловка, Ассоль | 20 — 350; 50 — 480; 100 — 600; 200 — 1000; 300 — 1500 | 7500; в Витино переподключение от другого провайдера бесплатно |
| G | Полет | 10 — 340; 50 — 480 | 7500 |
| H | Черноморец, Парус, Донузлав | 20 — 350; 50 — 480; 100 — 600 | 7500 |
| I | Огневое, Шалаши | 25 — 400; 50 — 600; 100 — 800 | 7500 |
| K | коммерческие летне-сезонные объекты Евпатории | 25 — 500; 50 — 1000; 100 — 1500; 200 — 2000; 300 — 3000 | не указано |

### 13.4. Населённые пункты

- A: квартиры Евпатории и Заозерного, многоэтажки Штормового.
- B: частный сектор Евпатории и Заозерного; Суворовское, Каменоломня, Автомобилист, СНТ Медик и Чайка-С (Агропром), Ленок, Сад Дружба, Строитель, Звезда, Днепр, Оазис, Восток, Таврида, Мечта, Сокол, Изобильное, Надежда; Исмаил-Бей, Спутник 1; Лимановка; Великое, Колоски, Наташино, Ромашкино; Спутник-2, Спутник-3, пос. Новый, Нахаловка, Воробьевка, Веселовка, Добрушино, Уютное; СНТ Садовод, Лиманное, Буревестник, Геолог, Отдых, Уют, Авиатор, Весна.
- Елизаветово использует линейку B, но подключение 7500 ₽.
- Для СНТ Садовод, Лиманное, Буревестник, Геолог, Отдых, Уют, Авиатор, Весна переподключение на оптоволокно бесплатно.
- Остальные соответствия указаны в таблице линеек.

### 13.5. «Сезонный»

- многоквартирный дом — 1000 ₽/мес;
- частный сектор — 1500 ₽/мес;
- предприниматели и нежилые помещения — 2500 ₽/мес.

После более чем 6 месяцев без оплаты абонент автоматически переводится на «Сезонный». Возврат на обычный тариф — после предоплаты за год обслуживания.

### 13.6. Дополнительные услуги и контакты

- «белый» IP — 100 ₽/мес;
- баннер 100×100 на сайте — 300 ₽/мес;
- телевидение в отдельных населённых пунктах — 150 ₽/мес;
- телефоны: +7 (978) 075-59-00, +7 (978) 048-96-64;
- часы из справочника: ежедневно 8:00–22:00;
- e-mail из справочника: `adm.evpanet@gmail.com`;
- оператор: 508.

## 14. Записи и метаданные

### 14.1. Файлы

При `RECORD_CALLS=true` на вызов создаются:

```text
RECORDINGS_DIR/<uuid>.wav
RECORDINGS_DIR/<uuid>.json
```

WAV содержит только входящий звук абонента после приведения к 8 кГц. Запись инкрементальная; файл сбрасывается на диск после каждого фрейма.

Текущее поведение: если WAV не открыт или запись отключена, JSON также не создаётся. Для production рекомендуется разделить запись аудио и метаданных.

### 14.2. Поля JSON

```json
{
  "session_id": "uuid",
  "channel_id": "ARI channel",
  "caller": "номер или null",
  "scenario": "default",
  "peer": "IP:port",
  "started_at": "UTC ISO-8601",
  "duration_seconds": 24.7,
  "audio_frames": 1000,
  "audio_seconds": 20.0,
  "steps": ["greeting", "dialog"],
  "transcripts": ["Какой у меня баланс?"],
  "partial_transcripts": ["частичный текст"],
  "barge_ins": 0,
  "ignored_echo": 0,
  "tts_characters": 100,
  "speech_errors": 0,
  "llm_turns": 1,
  "llm_tool_calls": 1,
  "llm_errors": 0,
  "llm_replies": ["ответ"],
  "hangup_reason": "llm_end_call",
  "billing": null,
  "recording": "uuid.wav",
  "voice_provider": "yandex_realtime",
  "realtime_model": "speech-realtime-max-tts-live/latest",
  "realtime_system_prompt_characters": 19627,
  "realtime_input_audio_seconds": 12.5,
  "realtime_output_audio_seconds": 4.2,
  "realtime_usage": {
    "responses": 1,
    "total_tokens": 1200,
    "input_tokens": 1000,
    "input_text_tokens": 700,
    "input_audio_tokens": 250,
    "cached_input_tokens": 600,
    "cached_input_text_tokens": 550,
    "cached_input_audio_tokens": 50,
    "uncached_input_tokens": 400,
    "tool_tokens": 50,
    "output_tokens": 200,
    "output_text_tokens": 20,
    "output_audio_tokens": 180
  },
  "realtime_first_audio_latency_ms": [510.0]
}
```

`voice_provider` записывается в обоих режимах. Остальные `realtime_*` поля добавляются только в Realtime. Секунды входа/выхода вычисляются по фактически переданному PCM 16 кГц до ресемплинга; токены суммируются из `response.usage` всех `response.done`. Кешированные токены являются подмножеством входных, `uncached_input_tokens` — разница между ними. `responses=0` означает, что провайдер не прислал usage. Денежные тарифы и автоматический расчёт рублей в sidecar не хранятся.

Типовые причины завершения: `hangup_step`, `dialog_end`, `llm_end_call`, `llm_transfer`, `realtime_end_call`, `realtime_transfer`, `realtime_error`, `realtime_input_error`, `terminated`, `peer_closed`, `idle_timeout`, `max_duration`, `asterisk_error`, `connection_error`, `cancelled`, `error`.

Данные являются чувствительными: необходимо ограничить доступ, установить срок хранения и удаление.

## 15. Конфигурация

Все пути относительно корня проекта, если не задан абсолютный путь. `ENV_FILE` или `--env-file` выбирает другой файл. Реальные секреты не коммитить.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ARI_URL` | нет | ARI REST base URL |
| `ARI_USER` | нет | ARI login |
| `ARI_PASSWORD` | нет | ARI password |
| `ARI_APP` | нет | Stasis application |
| `AUDIOSOCKET_HOST` | `0.0.0.0` | listen address |
| `AUDIOSOCKET_PORT` | `9092` | listen port |
| `EXTERNAL_MEDIA_HOST` | автоматически локальный IPv4 | адрес для Asterisk; в production задать явно |
| `EXTERNAL_MEDIA_PORT` | `AUDIOSOCKET_PORT` | внешний порт |
| `EXTERNAL_MEDIA_FORMAT` | `slin` | формат ExternalMedia |
| `EXTERNAL_MEDIA_TRANSPORT` | `tcp` | транспорт |
| `EXTERNAL_MEDIA_ENCAPSULATION` | `audiosocket` | encapsulation |
| `EXTERNAL_MEDIA_TIMEOUT` | `5` | ожидание TCP, с |
| `ALLOW_UNKNOWN_SESSIONS` | см. примечание | принимать неизвестные UUID |
| `SCENARIO_PATH` | `scenarios/default.json` | живой голосовой сценарий |
| `PROMPTS_DIR` | `prompts/placeholder` | WAV-промпты |
| `RECORDINGS_DIR` | `recordings` | записи |
| `RECORD_CALLS` | `true` | запись WAV |
| `INIT_TIMEOUT` | `10` | ожидание UUID, с |
| `IDLE_TIMEOUT` | `30` | отсутствие фреймов, с |
| `MAX_CALL_SECONDS` | `600` | лимит вызова, с |
| `BILLING_BASE_URL` | нет | endpoint |
| `BILLING_TOKEN` | нет | Bearer token |
| `BILLING_TIMEOUT` | `10` | timeout, с |
| `BILLING_MOCK` | `false` | тестовый биллинг |
| `VOICE_PROVIDER` | `aitunnel` | `aitunnel` или `yandex_realtime`; другое значение — ValueError |
| `YANDEX_API_KEY` | нет | Api-Key, маскируется в логах |
| `YANDEX_FOLDER_ID` | нет | folder для model URI |
| `YANDEX_REALTIME_URL` | `wss://ai.api.cloud.yandex.net/v1/realtime/openai` | WebSocket |
| `YANDEX_REALTIME_MODEL` | `speech-realtime-max-tts-live/latest` | модель LiveTTS |
| `YANDEX_REALTIME_VOICE` | `sofia` | голос |
| `YANDEX_REALTIME_TIMEOUT` | `10` | connect timeout, с |
| `YANDEX_DATA_LOGGING` | `false` | заголовок логирования содержимого |
| `AITUNNEL_API_KEY` | нет | единый ключ STT/TTS/LLM |
| `AITUNNEL_BASE_URL` | `https://api.aitunnel.ru/v1` | API base URL |
| `STT_MODEL` | `whisper-large-v3-turbo` | STT model |
| `SPEECH_LANGUAGE` | `ru` | язык |
| `SPEECH_TIMEOUT` | `20` | STT/TTS timeout, с |
| `TTS_MODEL` | `gpt-4o-mini-tts` | TTS model |
| `TTS_VOICE` | `nova` | голос |
| `TTS_FORMAT` | `pcm` | `pcm` или `wav` |
| `TTS_PCM_RATE` | `24000` | частота исходного PCM |
| `VOICE_INPUT` | `true` | VAD/STT |
| `VOICE_BARGE_IN` | `true` | голосовое прерывание |
| `VOICE_ECHO_GUARD` | `true` | защита от эха |
| `PLAYBACK_LEAD_IN_MS` | `150` | тишина перед фразой |
| `VAD_START_MS` | `160` | начало речи |
| `VAD_SILENCE_MS` | `700` | конец речи |
| `VAD_MIN_SPEECH_MS` | `200` | минимальная реплика |
| `VAD_RMS_THRESHOLD` | `300` | минимальный RMS |
| `VAD_NOISE_MULTIPLIER` | `2.5` | запас над шумом |
| `STT_PARTIAL_INTERVAL_MS` | `1200` | интервал partial STT |
| `LLM_MODEL` | `gpt-4o-mini` | LLM model |
| `LLM_PROMPTS_DIR` | `prompts/llm` | markdown prompts |
| `LLM_TEMPERATURE` | `0.3` | температура |
| `LLM_MAX_TOKENS` | `300` | лимит ответа |
| `LLM_TIMEOUT` | `30` | timeout, с |
| `LLM_MAX_TOOL_ROUNDS` | `4` | раунды tools |
| `LLM_HISTORY_MESSAGES` | `40` | история |
| `LOG_LEVEL` | `INFO` | логирование |

**Несоответствие:** в `Settings.from_env()` `ALLOW_UNKNOWN_SESSIONS` по умолчанию `True`, а в `.env.example` установлено `false`. Для ARI/production следует явно задавать `false`; при воссоздании лучше сразу исправить кодовый default на fail-closed.

ARI включается автоматически только при полном наборе `ARI_URL`, `ARI_USER`, `ARI_PASSWORD`, `ARI_APP`. Биллинг включён при URL + token или mock. В AITUNNEL STT/TTS/LLM требуют `AITUNNEL_API_KEY`; в Realtime при старте обязательны `YANDEX_API_KEY` и `YANDEX_FOLDER_ID`, иначе CLI завершается кодом 2. В Realtime AITUNNEL-клиенты не создаются и их ключ не нужен. `VAD_SILENCE_MS` задаёт серверную тишину, а остальные локальные VAD/partial-настройки относятся только к AITUNNEL.

## 16. CLI и диагностика

```powershell
python -m app
python -m app --scenario scenarios/llm-first.json
python -m app --no-ari --port 9092
python -m app --check
python -m app --ari-check
python -m app --billing-check 1
python -m app --billing-check 1 --billing-mock
python -m app --tts-check "Проверка синтеза речи"
python -m app --stt-check recordings/example.wav
python -m app --llm-check "Какие тарифы в Евпатории?"
python -m app --voice-provider yandex_realtime --check
python -m app --env-file .env.yandex
```

Дополнительные параметры:

- `--env-file PATH`;
- `--host`, `--port`, `--voice-provider aitunnel|yandex_realtime`;
- `--scenario`, `--prompts-dir`, `--llm-prompts-dir`;
- `--recordings-dir`;
- `--record` / `--no-record`;
- `--ari` / `--no-ari`;
- `--external-media-host`, `--external-media-port`;
- `--allow-unknown-sessions` / `--no-allow-unknown-sessions`;
- `--billing-mock` / `--no-billing-mock`;
- `--tts-output`;
- `--log-level`.

Инструменты:

```powershell
python tools/make_placeholder_prompts.py
python tools/simulate_call.py --input recordings/caller.wav --save recordings/reply.wav
python tools/check_external_media.py --host <внешний-адрес> --port 9092
python tools/check_realtime.py --env-file .env.yandex
python tools/save_tts_comparison.py "Здравствуйте! Проверка качества речи."
```

Симулятор передаёт WAV блоками 20 мс, добавляет `--tail-ms` тишины (по умолчанию 1000) для эндпоинтинга и слушает ответ. `--delay` задаёт паузу перед репликой.

`check_realtime.py` принудительно выбирает Yandex Realtime, загружает ключи из выбранного env-файла, подключается, отправляет `session.update` и ждёт `session.updated`. Секреты не печатаются; ошибки API показываются с описанием. Модель передаётся в обязательном query-параметре `model` и дублируется в session.

`save_tts_comparison.py` делает один TTS-запрос и сохраняет:

- `original-24000.wav` — исходный PCM в WAV;
- `current-8000.wav` — результат текущего потокового SoXR.

## 17. Структура репозитория

```text
app/
  __main__.py
  config.py
  logging_setup.py
  network.py
  ari/
  audio/
  audiosocket/
  billing/
  dialog/
  llm/
  realtime/
    client.py
    session.py
  speech/
asterisk/
  README.md
prompts/
  llm/
    support_evp.md
    billing_account_data.md
    connection_terms.md
    internet_private.md
  placeholder/             # генерируется, не коммитится
  ru/                      # реальные записи, если используются
scenarios/
  default.json
  llm-first.json
tests/
tools/
  check_external_media.py
  check_realtime.py
  make_placeholder_prompts.py
  save_tts_comparison.py
  simulate_call.py
.env.example
.gitignore
pyproject.toml
requirements.txt
README.md
PROJECT_SPEC.md
```

Не коммитить `.env`, `.env.*` (кроме `.env.example`), `recordings/`, `.venv`, pytest cache и реальные секретные заметки.

## 18. Ошибки и завершение

Общие принципы:

- ошибка одной сессии не должна ронять сервер;
- ошибка STT, TTS или LLM не должна оставлять необработанную фоновую задачу;
- при завершении отменять read/STT либо Realtime input/event/finalizer/playback tasks;
- закрывать recorder, writer, Realtime WebSocket и HTTP-сессию;
- отправлять AudioSocket terminate, если соединение ещё открыто;
- очищать pending registry, media channel, bridge и caller;
- останавливать ARI events и HTTP-сессии при shutdown;
- секреты в логах заменять на `***`.

Для production дополнительно требуется:

- ограничить время очистки ARI;
- не блокировать WebSocket event loop последовательными DELETE;
- гарантировать очистку ресурсов при частично неуспешном старте;
- ограничить очереди ввода и число одновременных соединений;
- валидировать диапазоны всех timeout/port/VAD-настроек.

## 19. Сетевые требования и безопасность

1. `EXTERNAL_MEDIA_HOST:EXTERNAL_MEDIA_PORT` должен быть доступен с Asterisk `91.195.230.13`.
2. Пробросить TCP-порт на машину бота, если используется NAT.
3. Разрешить входящий TCP 9092 только от доверенного Asterisk.
4. Открывать ARI-порт наружу со стороны бота не требуется: бот сам инициирует соединения.
5. AudioSocket сам по себе не аутентифицирован; UUID не заменяет сетевой allowlist.
6. Использовать `ALLOW_UNKNOWN_SESSIONS=false` в ARI-режиме.
7. Хранить секреты в секрет-хранилище или неотслеживаемом `.env`.
8. ARI без TLS допустим только в защищённой сети; при возможности перейти на HTTPS/WSS.
9. STT/TTS/LLM отправляют речь и текст внешнему провайдеру; это должно быть отражено в политике обработки данных.
10. Записи, номера, расшифровки и биллинг требуют ограничения доступа и срока хранения.
11. Не добавлять LLM-инструменты, изменяющие биллинг или инфраструктуру, без отдельной авторизации и подтверждения.
12. Перед выдачей данных счёта в production реализовать проверку личности/принадлежности счёта.

## 20. Тестирование и критерии приёмки

Запуск:

```powershell
python -m pytest
```

Обязательные группы:

- AudioSocket protocol, UUID, неполные фреймы и неизвестные типы;
- PCM/WAV, arbitrary chunks, SoXR streaming, anti-alias;
- темп воспроизведения и stop;
- сценарии, голосовые ссылки, неверные поля, timeout/fallback;
- голосовые интенты без числового меню;
- ARI REST, auth, reconnect, полный вызов и очистка;
- unknown UUID;
- биллинг: found/not found, 400/5xx, не-JSON, mock;
- VAD, pre-roll, partial STT, barge-in, echo guard;
- STT/TTS fake server, multipart и поток;
- LLM tools, история, лимит раундов, ошибки;
- Realtime: env/masking, WS URL/headers/session.update, flat tools, 8 ↔ 16 кГц, transcripts, latency, billing output, end/transfer после audio, barge-in и cleanup;
- выбор `RealtimeCallSession` только при явном `yandex_realtime`;
- живой диалог и fallback при отсутствии LLM-ключа или тишине.

### 20.1. Минимальная приёмка воссозданного проекта

1. `python -m app --check` завершается кодом 0.
2. Все автоматические тесты проходят.
3. Сквозной fake-ARI вызов доходит от `StasisStart` до очистки.
4. Неизвестный UUID отклоняется в ARI-режиме.
5. Телефонные кнопки не вызывают сценарных переходов.
6. Обрыв TCP не останавливает сервер.
7. TTS 24 кГц потоково преобразуется в 8 кГц без независимых границ фильтра.
8. Исходный и обработанный TTS можно сохранить и прослушать.
9. Свободная реплика проходит STT → LLM → TTS и сохраняется в метаданных.
10. Ошибки биллинга и LLM приводят к предсказуемому fallback.
11. Секреты не появляются в логах и репозитории.
12. Реальный звонок на 777 имеет двусторонний звук и корректно завершается.
13. Для Realtime моки подтверждают протокол, аудио, функции, barge-in и отсутствие зависших задач; отдельный живой звонок остаётся обязательным перед эксплуатацией.

### 20.2. Отдельная production-приёмка

Эти пункты не выполнены текущей версией полностью:

- оператор реально получает вызов;
- занято/нет ответа оператора обрабатывается;
- доступ к чужому счёту невозможен;
- raw billing не сохраняется;
- записи автоматически удаляются по политике;
- есть метрики задержек и ошибок;
- проведена нагрузочная проверка нескольких одновременных вызовов.

## 21. Развёртывание с нуля

1. Установить Python 3.11+.
2. Создать структуру каталогов по разделу 17.
3. Установить зависимости:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
```

4. Скопировать `.env.example` в `.env`.
5. Заполнить `ARI_*`, `EXTERNAL_MEDIA_HOST`, `AITUNNEL_API_KEY`, биллинг.
6. Явно установить `ALLOW_UNKNOWN_SESSIONS=false`.
7. Настроить Asterisk Stasis/ARI и ExternalMedia по разделам 5–6.
8. Пробросить и ограничить AudioSocket-порт.
9. Создать тоновые промпты или подготовить реальные WAV:

```powershell
python tools/make_placeholder_prompts.py
```

10. Загрузить LLM-промпты и проверить актуальность знаний из раздела 13.
11. Выполнить:

```powershell
python -m app --check
python -m pytest
python -m app --ari-check
python tools/check_external_media.py --host <внешний-адрес> --port 9092
```

12. Проверить STT/TTS/LLM диагностическими командами.
13. Выполнить тестовый звонок, проверить живой диалог, запись, метаданные и очистку ARI.
14. Для Realtime заполнить Yandex key/folder в том же `.env`, остановить AITUNNEL и запустить `python -m app --voice-provider yandex_realtime`; сохранить `ivr_ai`, 9092 и номер 777.
15. До production закрыть пункты раздела 20.2.

## 22. Рекомендуемый порядок развития

1. Реальный перевод оператору и обработка занято/нет ответа.
2. Авторизация доступа к данным счёта.
3. Минимизация биллинга и ПДн в JSON/логах, retention.
4. Надёжность ARI event loop, startup/shutdown и лимиты соединений.
5. Буфер воспроизведения TTS и метрики задержек/underrun.
6. CI, Ruff, типизация, lock-файл зависимостей.
7. Декомпозиция `CallSession`.
8. Выбор справочников по намерению; RAG только при росте базы знаний.
9. PostgreSQL и контейнеризованная тестовая АТС по фактической потребности.

---

## 23. Источник истины при расхождениях

При расхождении документа и реализации:

1. зафиксировать расхождение;
2. определить, является ли текущее поведение ошибкой или требованием;
3. обновить код и тесты либо этот документ;
4. не считать известные ограничения раздела 2.3 правильным целевым поведением;
5. не хранить в спецификации секреты и данные реальных абонентов.
