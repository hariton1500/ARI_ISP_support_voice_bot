# Голосовой бот службы поддержки абонентов (Asterisk + AudioSocket)

Бот для автоматического приёма звонков в службу поддержки оператора связи на
русском языке. Общается с Asterisk по протоколу **AudioSocket**: Asterisk сам
подключается к боту по TCP и передаёт звук в обе стороны.

## Текущий этап

Реализован **каркас без ИИ**:

* TCP-сервер AudioSocket на asyncio (внешние зависимости — только `numpy` для
  ресемплинга и `python-dotenv` для `.env`);
* сценарий диалога в JSON: приветствие, голосовое меню, переходы по DTMF,
  повтор шага при неверном вводе, таймауты, завершение вызова;
* проигрывание WAV-промптов с реальным темпом (20 мс на фрейм) и
  перебивание промпта нажатием цифры;
* запись входящего звука в WAV + JSON-метаданные вызова;
* тесты: протокол, аудио, сценарий, сквозной вызов через реальный сокет.

Распознавание речи, синтез и LLM подключаются на следующем этапе — точки
расширения заложены в структуре пакетов (`app/dialog`, `app/audio`).

## Архитектура

```
   абонент ──SIP/RTP──> Asterisk ──AudioSocket (TCP)──> Python-бот
                            │                              │
                            │  TCP: UUID, PCM 8 кГц,       ├─ сценарий диалога (JSON)
                            │  DTMF                      ├─ промпты WAV → PCM
                            ▼                              ├─ запись входящего звука
                     канал связи                          └─ метрики/логи
```

Ключевые особенности AudioSocket (подробнее — в
[документации Asterisk](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)):

| Фрейм | Назначение |
|---|---|
| `0x00` | завершение соединения (достаточно закрыть сокет) |
| `0x01` | UUID канала — 16 бинарных байт |
| `0x03` | DTMF-цифра (Asterisk 18+, один ASCII-байт) |
| `0x10` | PCM 16 бит, моно, little-endian, 8 кГц (остальные `0x11`–`0x18` — 12…192 кГц) |
| `0xFF` | ошибка |

Заголовок фрейма — 3 байта: тип (1 байт) + длина payload (2 байта, big-endian).
Приложение `AudioSocket(uuid,host:port)` **не отвечает** на вызов само, поэтому в
диалплане обязателен `Answer()`.

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env      # и впишите свои значения (секреты в git не попадают)
python tools/make_placeholder_prompts.py   # заглушки промптов (тоны вместо речи)
python -m app --check                      # проверка сценария и промптов
python -m app                              # запуск: слушаем 0.0.0.0:9092
```

Ожидаемый лог:

```
2026-10-30 12:00:00 INFO  [app.audiosocket.server] AudioSocket слушает: ('0.0.0.0', 9092)
2026-10-30 12:00:00 INFO  [app] бот готов: сценарий 'default' (10 шагов), промптов 9, порт 9092
```

Проверка без Asterisk — тестами (они поднимают сервер на свободном порту):

```powershell
python -m pytest
```

Либо «живьём» симулятором Asterisk: он подключается как АТС, слушает промпты и
нажимает DTMF.

```powershell
# терминал 1
python -m app --port 9092
# терминал 2
python tools/simulate_call.py --digits 0 --save recordings/prompts.wav
```

Ожидаемый вывод симулятора:

```
канал 6cfc6f50-...: подключились к 127.0.0.1:9092
-> DTMF 0
фреймы от бота: {'AUDIO_8K': 161, 'TERMINATE': 1}
получено аудио: 3.22 с
промпты сохранены: recordings/prompts.wav
```

## Подключение Asterisk

1. Убедитесь, что загружен `chan_audiosocket` (см. `asterisk/modules.conf.sample`).
2. Добавьте контекст из `asterisk/extensions.conf.sample`:

```asterisk
[ivr_ai]
exten => 777,1,NoOp(IVR AI: входящий вызов от ${CALLERID(num)})
 same => n,Answer()
 same => n,Set(CHANNEL(language)=ru)
 same => n,AudioSocket(${UUID()},127.0.0.1:9092)
 same => n,Hangup()
```

3. Перезагрузите диалплан: `dialplan reload` (модуль: `module reload chan_audiosocket.so`).
4. Позвоните на 777 — бот должен проиграть приветствие и меню.

Адрес в `AudioSocket(...)` — это машина с ботом; порт должен совпадать с
`AUDIOSOCKET_PORT`. Если Asterisk и бот на разных хостах, откройте порт в
файрволе и ограничьте доступ по IP АТС: незашифрованный AudioSocket не
аутентифицирован.

## Настройки (.env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `AUDIOSOCKET_HOST` | `0.0.0.0` | адрес прослушивания AudioSocket |
| `AUDIOSOCKET_PORT` | `9092` | порт AudioSocket (его указывают в диалплане) |
| `SCENARIO_PATH` | `scenarios/default.json` | файл сценария диалога |
| `PROMPTS_DIR` | `prompts/placeholder` | каталог промптов (для реальных записей — `prompts/ru`) |
| `RECORDINGS_DIR` | `recordings` | каталог записей и метаданных |
| `RECORD_CALLS` | `true` | писать входящий звук в WAV |
| `INIT_TIMEOUT` | `10` | сколько ждать фрейм UUID от Asterisk, с |
| `IDLE_TIMEOUT` | `30` | обрыв, если от АТС нет ни одного фрейма, с |
| `MAX_CALL_SECONDS` | `600` | жёсткий лимит длительности вызова, с |
| `LOG_LEVEL` | `INFO` | уровень логирования |
| `ARI_URL`, `ARI_USER`, `ARI_PASSWORD`, `ARI_APP` | — | параметры ARI для следующего этапа |

Пароль ARI маскируется в логах (фильтр `app.logging_setup.SecretFilter`).

## Сценарий диалога

Сценарий — JSON с именованными шагами (`scenarios/default.json`):

```json
{
  "name": "default",
  "initial": "greeting",
  "steps": {
    "greeting": {"label": "приветствие", "prompt": "greeting.wav", "next": "menu"},
    "menu": {
      "label": "главное меню",
      "prompt": "menu.wav",
      "timeout_ms": 8000,
      "max_attempts": 2,
      "dtmf": {"1": "balance", "2": "tech_support", "3": "payment", "0": "operator"},
      "on_invalid": "invalid_digit",
      "on_timeout": "no_input"
    },
    "operator": {"label": "оператор", "prompt": "operator.wav", "hangup": true}
  }
}
```

Поля шага:

| Поле | Смысл |
|---|---|
| `prompt` | имя файла промпта относительно `PROMPTS_DIR` |
| `next` | переход после проигрывания промпта (если нет ожидания DTMF) |
| `dtmf` | карта «цифра → шаг» |
| `timeout_ms` | сколько ждать цифру (по умолчанию 8000) |
| `max_attempts` | сколько раз повторить шаг при неверном вводе (по умолчанию 2) |
| `on_invalid` | шаг при неверной цифре после исчерпания попыток |
| `on_timeout` | шаг при отсутствии ввода после исчерпания попыток |
| `hangup` | завершить вызов после промпта |

Логика ввода: верная цифра → переход по `dtmf`; неверная цифра или тишина →
повтор шага; после `max_attempts` попыток → `on_invalid`/`on_timeout`
(если нужного перехода нет — `on_timeout`, если нет и его — конец диалога).
Сценарий валидируется при старте: неизвестные ссылки, кривые цифры и
отрицательные таймауты дают понятную ошибку и код возврата 2.

## Промпты

Поддерживаются:

* **WAV** (PCM 8/16/24/32 бита, любое число каналов, любая частота) —
  приводится к 8 кГц, моно, 16 бит при загрузке;
* **сырой PCM Asterisk**: `.sln` (8 кГц), `.sln16` (16 кГц), `.pcm`, `.raw`.

`prompts/placeholder` — сгенерированные тоновые заглушки (в git не попадают).
Реальные промпты диктора или TTS кладите в `prompts/ru` и указывайте
`PROMPTS_DIR=prompts/ru`. Отсутствующий промпт не роняет бота: шаг просто
прозвучит «в тишину», а в лог уйдёт предупреждение.

## Записи и метаданные

На каждый вызов в `RECORDINGS_DIR` появляются:

* `<uuid-канала>.wav` — входящий звук абонента (8 кГц, моно, 16 бит);
* `<uuid-канала>.json` — сценарий, peer, время начала, длительность, число
  аудиофреймов, набранные DTMF, пройденные шаги, причина завершения.

```json
{
  "session_id": "0c4c6b1e-...",
  "scenario": "default",
  "peer": "10.10.0.15:5123",
  "duration_seconds": 24.7,
  "audio_seconds": 24.6,
  "dtmf": "0",
  "steps": ["greeting", "menu", "operator"],
  "hangup_reason": "hangup_step",
  "recording": "0c4c6b1e-....wav"
}
```

Причины завершения: `hangup_step`, `dialog_end`, `terminated`, `peer_closed`,
`idle_timeout`, `max_duration`, `no_uuid`, `asterisk_error`,
`connection_error`, `cancelled`, `error`.

## Структура проекта

```
app/
  __main__.py            запуск (python -m app), флаг --check
  config.py              настройки из .env и переменных окружения
  logging_setup.py       логирование + маскирование секретов
  audiosocket/
    protocol.py          фреймы AudioSocket: разбор и сборка
    server.py            TCP-сервер, по сессии на соединение
    session.py           вызов: приём фреймов, диалог, запись
  audio/
    pcm.py               PCM16, ресемплинг, загрузка WAV/sln
    playback.py          проигрывание 20-мс фреймами, перебивание
    recorder.py          запись входящего звука в WAV
  dialog/
    scenario.py          модель и валидация сценария
    prompts.py           загрузка промптов сценария
asterisk/                примеры extensions/modules/ari/http.conf
scenarios/default.json   сценарий по умолчанию
tools/
  make_placeholder_prompts.py  генератор тоновых заглушек промптов
  simulate_call.py             симулятор Asterisk для проверки без АТС
tests/                   тесты, включая сквозной вызов по TCP
```

## Тесты

```powershell
python -m pytest
python -m pytest tests/test_protocol.py -v
```

Покрыто: форматирование/чтение фреймов, таймауты и обрывы, ресемплинг и WAV,
проигрыватель с перебиванием, запись, валидация сценария, загрузка промптов,
настройки, CLI и сквозной сценарий «подключились → услышали меню → нажали 0 →
вызов завершён, запись и метаданные созданы».

## Дальнейшие этапы

1. **ARI-слой**: подключение к `ws://.../ari/events?app=ivr_ai`, обработка
   `StasisStart`/`StasisEnd`/`ChannelDtmfReceived`, `POST /channels/{id}/continue`
   на строку `AudioSocket(...)`, перевод на оператора через `redirect`/очередь.
2. **Речь**: потоковый STT (ru), VAD и эндпоинтинг, TTS с выдачей 8 кГц,
   barge-in по голосу, а не только по DTMF.
3. **Диалог**: LLM + инструменты (баланс, статус услуги, заявка на выезд),
   RAG по регламентам и базе знаний.
4. **Эксплуатация**: PostgreSQL для логов диалогов, метрики latency/качества
   распознавания, docker-compose с тестовым Asterisk, нагрузочные тесты.

## Безопасность

* `.env` в `.gitignore`; в репозитории только `.env.example`.
* Пароль ARI, который ранее лежал в `.env` в открытом виде, **надо сменить** —
  он уже был скомпрометирован.
* ARI в примерах слушает `127.0.0.1`; для внешнего доступа включайте TLS
  (`https`/`wss`) — иначе пароль и события вызова идут открытым текстом.
* AudioSocket не аутентифицирован: порт бота должен быть доступен только с
  адреса Asterisk.
* Записи разговоров содержат персональные данные — ограничьте доступ к
  `RECORDINGS_DIR` и задайте срок хранения согласно политике оператора.
