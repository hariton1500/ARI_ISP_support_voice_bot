# Голосовой бот службы поддержки абонентов (Asterisk ARI + AudioSocket)

Бот принимает звонки на номер **777** и ведёт голосовое меню на русском языке.
Управление вызовом идёт через **ARI** (WebSocket-события + REST), звук — через
**ExternalMedia** с `encapsulation=audiosocket`: Asterisk сам подключается к
боту по TCP и передаёт PCM 8 кГц в обе стороны.

## Как это работает

Сторона Asterisk (`sip2.evpanet.com`, Asterisk 22.5) уже настроена: звонок на 777
попадает в Stasis-приложение `ivr_ai`. Менять диалплан не нужно.

```
бот                                                    Asterisk 22.5
── WS ws://…:8088/ari/events?app=ivr_ai ─────────────▶  держим постоянно
◀─ StasisStart {channel.id, caller.number} ──────────  звонок на 777
── POST /ari/channels/{caller}/answer ───────────────▶
── POST /ari/bridges?type=mixing ────────────────────▶  bridgeId
── POST /ari/channels/externalMedia?app=ivr_ai
     &external_host=<наш адрес>:9092
     &encapsulation=audiosocket&transport=tcp
     &format=slin&data=<uuid> ────────────────────────▶  emId
◀─ TCP-подключение от Asterisk ───────────────────────  1-й фрейм 0x01 = uuid
── POST /ari/bridges/{bridgeId}/addChannel?channel=caller,em ▶
◀─ ChannelDtmfReceived {digit} ───────────────────────  цифры приходят по ARI
── DELETE /ari/channels/{caller}, /channels/{em}, /bridges/{bridgeId} ▶
```

Важные следствия:

* **Пока WebSocket не подключён, звонок на 777 сразу завершается** — Asterisk не
  знает, кому его отдать. Это первое, что нужно проверить при «вызов отклонён».
* `POST /channels/{id}/continue` не используется: он выводит канал из Stasis.
* DTMF в ARI-режиме читаются из события `ChannelDtmfReceived`. Asterisk
  дублирует цифру ещё и фреймом `0x03` в AudioSocket — такие фреймы
  игнорируются, иначе шаг срабатывает дважды (в автономном режиме фрейм
  остаётся единственным источником цифр).
* Связь TCP-соединения с вызовом — по `data=<uuid>`: Asterisk присылает его
  первым фреймом. В ARI-режиме соединения с неизвестным UUID отклоняются.
* Кодек — `slin`: 16 бит, моно, little-endian, **8 кГц**.

Фреймы AudioSocket
([документация](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)):

| Фрейм | Назначение |
|---|---|
| `0x00` | завершение соединения (достаточно закрыть сокет) |
| `0x01` | UUID — 16 бинарных байт |
| `0x03` | DTMF-цифра (в ARI-режиме дублирует `ChannelDtmfReceived` и игнорируется) |
| `0x10` | PCM 16 бит, моно, LE, 8 кГц (остальные `0x11`–`0x18` — 12…192 кГц) |
| `0xFF` | ошибка |

Заголовок — 3 байта: тип (1 байт) + длина payload (2 байта, big-endian).

## Текущий этап

* ARI-слой: WS-события с реконнектом, REST-команды, бридж, ExternalMedia,
  привязка TCP к вызову, DTMF из ARI, очистка каналов/бриджа;
* сценарий диалога в JSON: приветствие, меню, переходы по DTMF, повтор при
  неверном вводе, таймауты, завершение вызова;
* проигрывание WAV-промптов с реальным темпом (20 мс) и перебивание нажатием;
* запись входящего звука в WAV + JSON-метаданные вызова;
* запрос в биллинг по `userId` (баланс, тариф, дни, состояние сети) на шаге
  `balance`; результат уходит в лог и в метаданные вызова;
* 79 тестов, включая сквозной прогон «StasisStart → ExternalMedia → TCP по UUID
  → DTMF → очистка» на фейковом ARI и контракт биллинга.

Распознавание речи, синтез и LLM — следующий этап (точки расширения:
`app/dialog`, `app/audio`).

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env          # впишите ARI_PASSWORD и EXTERNAL_MEDIA_HOST
python tools/make_placeholder_prompts.py   # тоновые заглушки промптов
python -m app --ari-check       # REST + WebSocket ARI
python -m app --billing-check 1 # абонент из биллинга (userId=1)
python -m app --check           # сценарий и промпты
python -m app                   # бот: ARI + AudioSocket
```

Ожидаемый лог:

```
INFO [app.audiosocket.server] AudioSocket слушает: ('0.0.0.0', 9092)
INFO [app] бот готов: сценарий 'default' (10 шагов), промптов 9, AudioSocket порт 9092, адреса: 172.18.185.162
INFO [app.ari.events] ARI WebSocket подключён: ws://sip2.evpanet.com:8088/ari/events?app=ivr_ai
INFO [app.ari.manager] ARI: приложение 'ivr_ai', события ws://…, ExternalMedia -> <адрес>:9092
```

Полезные флаги: `--no-ari` (автономный режим без Asterisk), `--port`,
`--external-media-host`, `--allow-unknown-sessions`, `--log-level DEBUG`,
`--billing-check USER_ID`, `--billing-mock`.

## Сетевое требование (обязательно)

Asterisk подключается к боту сам, поэтому `EXTERNAL_MEDIA_HOST:EXTERNAL_MEDIA_PORT`
должен быть **доступен с 91.195.230.13**:

1. на роутере — проброс TCP-порта (по умолчанию 9092) на эту машину;
2. в Windows — разрешить входящий TCP 9092 (PowerShell **от администратора**):

```powershell
New-NetFirewallRule -DisplayName "ARI bot AudioSocket 9092" -Direction Inbound `
  -Protocol TCP -LocalPort 9092 -Action Allow -Profile Any
```

3. в `.env` — внешний адрес/домен и **внешний** порт, если проброс не 1:1
   (`EXTERNAL_MEDIA_PORT` — это порт, который набирает Asterisk).

Проверка доставки (бот должен быть остановлен, порт свободен):

```powershell
python tools/check_external_media.py --host <внешний-адрес> --port 9092
```

Инструмент открывает WebSocket ARI, просит Asterisk создать временный
медиаканал на указанный адрес, ждёт TCP с нашим UUID и удаляет канал. Успех —
`УСПЕХ: Asterisk подключился и прислал ожидаемый UUID`.

Порт 8088 (ARI) открывать не нужно — соединение инициируем мы; на стороне
Asterisk наш IP уже в белом списке.

## Диагностика

| Симптом | Причина и что делать |
|---|---|
| звонок на 777 сразу обрывается | нет WS-подключения к ARI: бот не запущен / не настроен ARI / IP не в белом списке. Запустите `python -m app --ari-check` |
| `externalMedia: 500` в логе | Asterisk не смог подключиться к `EXTERNAL_MEDIA_HOST:порт` — нет проброса или блокирует файрвол |
| «externalMedia не подключился … за 5 с» | то же: TCP не дошёл до бота |
| вызов идёт, но звука нет | проверьте `format=slin` и строку «медиапоток подключён»; смотрите лог шагов диалога |
| «UUID … не связан ни с одним вызовом» | TCP пришёл с неизвестным `data`; в ARI-режиме такие соединения отклоняются |
| «медиаканал … завершился (событие …), канал абонента ещё активен» | Asterisk закрыл `AudioSocket/…` раньше абонента: смотрите в логе АТС строки вокруг `Called …`/`is hung up`. Часто это вызов, завершённый на стороне оператора/провайдера |
| «биллинг: абонент … не найден» | `userId` не совпал с лицевым счётом: проверьте `billing` в шаге или `python -m app --billing-check <id>` |
| «биллинг: нет связи…/HTTP 401/500» | проверьте `BILLING_BASE_URL`, `BILLING_TOKEN`, доступность сервиса; в этом случае играется `prompt_error` |

## Настройки (.env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ARI_URL` | — | `http://sip2.evpanet.com:8088/ari` |
| `ARI_USER` / `ARI_PASSWORD` / `ARI_APP` | — | учётка и Stasis-приложение `ivr_ai` |
| `AUDIOSOCKET_HOST` | `0.0.0.0` | адрес прослушивания AudioSocket |
| `AUDIOSOCKET_PORT` | `9092` | локальный порт AudioSocket |
| `EXTERNAL_MEDIA_HOST` | — | адрес, доступный с Asterisk (`external_host`) |
| `EXTERNAL_MEDIA_PORT` | = `AUDIOSOCKET_PORT` | порт, который набирает Asterisk |
| `EXTERNAL_MEDIA_FORMAT` | `slin` | формат медиа (8 кГц, 16 бит, моно) |
| `EXTERNAL_MEDIA_TIMEOUT` | `5` | ожидание TCP от Asterisk, с |
| `ALLOW_UNKNOWN_SESSIONS` | `false` | принимать TCP без зарегистрированного вызова (отладка) |
| `SCENARIO_PATH` | `scenarios/default.json` | файл сценария |
| `PROMPTS_DIR` | `prompts/placeholder` | каталог промптов (реальные — `prompts/ru`) |
| `RECORDINGS_DIR` | `recordings` | записи и метаданные |
| `RECORD_CALLS` | `true` | писать входящий звук |
| `INIT_TIMEOUT` | `10` | ожидание UUID в AudioSocket, с |
| `IDLE_TIMEOUT` | `30` | обрыв при отсутствии фреймов, с |
| `MAX_CALL_SECONDS` | `600` | лимит длительности вызова, с |
| `BILLING_BASE_URL` | — | эндпоинт биллинга (`POST {"userId": ...}`) |
| `BILLING_TOKEN` | — | Bearer-токен биллинга |
| `BILLING_TIMEOUT` | `10` | таймаут запроса в биллинг, с |
| `BILLING_MOCK` | `false` | отдавать тестовые данные биллинга без сети |
| `LOG_LEVEL` | `INFO` | уровень логирования |

Пароль ARI и токен биллинга маскируются в логах
(`app.logging_setup.SecretFilter` и `Settings.masked()`).

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

| Поле шага | Смысл |
|---|---|
| `prompt` | файл промпта относительно `PROMPTS_DIR` |
| `next` | переход после промпта (если не ждём DTMF) |
| `dtmf` | карта «цифра → шаг» |
| `timeout_ms` | ожидание цифры (по умолчанию 8000) |
| `max_attempts` | попытки при неверном вводе (по умолчанию 2) |
| `on_invalid` | шаг после исчерпания попыток при неверной цифре |
| `on_timeout` | шаг при отсутствии ввода |
| `hangup` | завершить вызов после промпта |
| `billing` | перед промптом запросить биллинг: `"caller"` (номер звонящего) или конкретный `userId` |
| `prompt_not_found` | промпт, если абонент не найден (по умолчанию — `prompt`) |
| `prompt_error` | промпт, если биллинг недоступен (по умолчанию — `prompt`) |

Верная цифра → переход по `dtmf`; неверная цифра или тишина → повтор шага;
после `max_attempts` → `on_invalid`/`on_timeout` (если перехода нет —
`on_timeout`, если нет и его — конец диалога). Сценарий валидируется при
старте: неизвестные ссылки, кривые цифры и отрицательные таймауты дают ошибку
и код возврата 2.

## Биллинг

Шаг с полем `billing` сначала спрашивает биллинг, потом играет промпт:

```json
"balance": {
  "label": "баланс из биллинга",
  "prompt": "balance.wav",
  "prompt_not_found": "balance_not_found.wav",
  "prompt_error": "balance_error.wav",
  "billing": "caller",
  "next": "menu"
}
```

* `"billing": "caller"` — `userId` берётся из номера звонящего (ARI `caller.number`);
  можно указать конкретный идентификатор строкой: `"billing": "1"`;
* найдено → играется `prompt`, в лог уходит строка
  `биллинг: абонент '1' — баланс 227.23 руб., тариф «Интернет 100 Мбит», …`,
  а данные попадают в `<uuid>.json` (`billing`);
* не найдено → `prompt_not_found`, ошибка сети/HTTP → `prompt_error`;
* запрос идёт по контракту `POST {"userId": "..."}` + `Authorization: Bearer …`
  (см. `app/billing/client.py`), таймаут — `BILLING_TIMEOUT`.

Проверить связку без звонка:

```powershell
python -m app --billing-check 1     # код 0 — абонент найден, 1 — нет/ошибка, 2 — не настроен
python -m app --billing-check 503   # тестовый номер: "не найден"
python -m app --billing-check 1 --billing-mock   # тестовые данные без сети
```

Сумма пока не проговаривается голосом: в проекте только тоновые заглушки
промптов, поэтому шаг сообщает об успехе/неудаче запроса. Для озвучивания
суммы нужны либо записи диктора с цифрами, либо TTS (следующий этап).

## Промпты

* **WAV** — PCM 8/16/24/32 бита, любое число каналов и любая частота
  (приводится к 8 кГц, моно, 16 бит при загрузке);
* **сырой PCM Asterisk**: `.sln` (8 кГц), `.sln16` (16 кГц), `.pcm`, `.raw`.

`prompts/placeholder` — сгенерированные тоновые заглушки (в git не попадают).
Реальные записи диктора или TTS кладите в `prompts/ru` и указывайте
`PROMPTS_DIR=prompts/ru`. Отсутствующий промпт не роняет бота: шаг прозвучит
«в тишину», а в лог уйдёт предупреждение.

## Записи и метаданные

На каждый вызов в `RECORDINGS_DIR` появляются:

* `<uuid>.wav` — входящий звук абонента (8 кГц, моно, 16 бит), где `uuid` —
  значение `data` из ExternalMedia;
* `<uuid>.json` — сценарий, канал ARI, номер абонента, время, длительность,
  число аудиофреймов, DTMF, шаги, причина завершения и ответ биллинга
  (`billing`: `outcome`, `user_id`, баланс, тариф, дни, состояние сети).

```json
{
  "session_id": "0c4c6b1e-...",
  "channel_id": "1793357082.75",
  "caller": "79001112233",
  "scenario": "default",
  "duration_seconds": 24.7,
  "dtmf": "0",
  "steps": ["greeting", "menu", "balance", "menu", "operator"],
  "hangup_reason": "hangup_step",
  "billing": {
    "outcome": "found",
    "user_id": "1",
    "balance": 227.23,
    "packet_name": "Интернет 100 Мбит",
    "days_left": 12,
    "network_state": "green"
  },
  "recording": "0c4c6b1e-....wav"
}
```

Причины завершения: `hangup_step`, `dialog_end`, `terminated`, `peer_closed`,
`idle_timeout`, `max_duration`, `asterisk_error`, `connection_error`,
`cancelled`, `error`.

## Проверка без АТС

```powershell
python -m pytest                 # 79 тестов, фейковый ARI, TCP и биллинг
```

Автономный режим (без Asterisk, для отладки сценария):

```powershell
python -m app --no-ari --port 9092
python tools/simulate_call.py --digits 0 --save recordings/prompts.wav
```

Симулятор подключается как АТС, слушает промпты и нажимает DTMF.

## Структура проекта

```
app/
  __main__.py            запуск, --ari-check, --billing-check, --check, --no-ari
  config.py              настройки из .env
  logging_setup.py       логи + маскирование секретов
  network.py             локальные IPv4 для подсказок
  ari/
    client.py            REST ARI (answer, bridges, externalMedia, delete)
    events.py            WebSocket событий с реконнектом
    manager.py           оркестрация вызова и очистка
  audiosocket/
    protocol.py          фреймы AudioSocket, чтение UUID
    registry.py          связка «UUID ↔ вызов ARI»
    server.py            TCP-сервер, по сессии на соединение
    session.py           диалог, промпты, запись, DTMF, запрос в биллинг
  billing/
    client.py            POST {"userId": ...} + Bearer, разбор ответа
  audio/                 PCM, ресемплинг, проигрыватель, запись
  dialog/                сценарий и промпты
asterisk/README.md       что настроено на стороне Asterisk
scenarios/default.json   сценарий по умолчанию
tools/
  make_placeholder_prompts.py  заглушки промптов
  simulate_call.py             симулятор АТС (автономный режим)
  check_external_media.py      проверка доступности ExternalMedia
tests/                   79 тестов
```

## Тесты

```powershell
python -m pytest
```

Покрыто: протокол и аудио, сценарий и промпты, настройки и CLI, ARI REST-клиент,
клиент биллинга (контракт, 400/5xx/не-JSON, mock), выбор промпта по ответу
биллинга, реконнект WebSocket, реестр ожидающих вызовов, сквозной вызов
«StasisStart → бридж → ExternalMedia → AudioSocket по UUID → DTMF → очистка»,
отклонение неизвестного UUID, автономный режим с симулятором.

## Безопасность

* `.env` в `.gitignore`; в репозитории только `.env.example`.
* `ari-ivr_ai.md` содержит пароль ARI в открытом виде — файл добавлен в
  `.gitignore`; пароль **надо сменить**, он уже раскрыт.
* По требованию Asterisk HTTP/WS работают без TLS, доступ — только по белому
  списку IP, поэтому пароль нельзя публиковать.
* AudioSocket не аутентифицирован: разрешайте входящие соединения только с
  `91.195.230.13`.
* Записи разговоров содержат персональные данные — ограничьте доступ к
  `RECORDINGS_DIR` и задайте срок хранения.

## Дальнейшие этапы

1. STT/TTS на русском (потоково), VAD и эндпоинтинг, barge-in по голосу.
2. LLM + инструменты (баланс, статус услуги, заявка на выезд), RAG по регламентам.
3. PostgreSQL для логов диалогов, метрики задержек, docker-compose с тестовой АТС.
4. Перевод на TLS-эндпоинт ARI, когда провайдер его включит.
