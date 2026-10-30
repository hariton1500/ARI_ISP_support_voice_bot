# Сторона Asterisk

Диалплан и конфигурация ARI находятся у провайдера (`sip2.evpanet.com`,
Asterisk 22.5.2) и **менять их не нужно**. Проверено с нашей стороны запросом
`GET /ari/asterisk/info` (HTTP 200).

## Что настроено на стороне Asterisk

| Параметр | Значение |
|---|---|
| Хост | `sip2.evpanet.com` (91.195.230.13) |
| Порт (ARI REST + WebSocket) | `8088`, HTTP без TLS |
| WebSocket событий | `ws://sip2.evpanet.com:8088/ari/events?app=ivr_ai` |
| REST | `http://sip2.evpanet.com:8088/ari/...` |
| Stasis-приложение | `ivr_ai` |
| Логин | `ivr_ai` (пароль — в `.env`, не публиковать) |
| Тестовый номер | `777` |
| Версия | Asterisk 22.5.2 |

Загружены `app_audiosocket.so`, `chan_audiosocket.so`, `res_audiosocket.so`,
`res_ari_channels.so` (проверено через `GET /ari/asterisk/modules`).

## Что нужно от нас

1. Держать WebSocket `/ari/events?app=ivr_ai` открытым — иначе звонки на 777
   сразу завершаются.
2. Отвечать на вызов (`POST /channels/{id}/answer`).
3. Создавать `POST /channels/externalMedia` с `encapsulation=audiosocket`,
  `transport=tcp`, `format=slin` и `external_host` = наш адрес:порт.
4. Слушать TCP на этом порту и принимать первым фреймом `data`-UUID.
5. Добавлять каналы в бридж и завершать вызов через `DELETE`.

Диалплан и конфигурацию ARI на стороне Asterisk менять не нужно.
