# Seedance 2 Video Studio

Локальный web-сервис для генерации видео через Seedance 2 compatible async API.

## Возможности

- Генерация через BytePlus Ark Seedance 2.0, как в Yango Perf.
- Text-to-video через `seedanceapi.org/v2`.
- Поддержка reAPI `doubao-seedance-2.0` variants.
- Image-to-video, first/last frame, reference video/audio inputs where provider supports them.
- Upload reference image/video/audio files and serve them as public `/uploads/...` URLs.
- Polling статуса задачи и предпросмотр готового MP4 в браузере.
- Чтение API ключей из `~/Desktop/tokens.txt` и переменных окружения.

## Ключи

Сервис ищет ключи в переменных окружения и в `~/Desktop/tokens.txt`.

Для SD 2.0 API:

```env
SEEDANCE_API_KEY="your_key"
```

Для BytePlus Ark, основной вариант:

```env
ARK_API_KEY="your_key"
```

Также поддерживаются:

```env
BYTEPLUS_ARK_API_KEY="your_key"
BYTEPLUS_API_KEY="your_key"
SEEDANCE_API_KEY="your_key"
```

Для reAPI:

```env
REAPI_API_KEY="your_key"
```

Если ключа нет, его можно временно ввести в поле `API key override` в UI.

## Запуск

```bash
python3 web_app.py
```

По умолчанию сервис доступен на:

```text
http://127.0.0.1:8080
```

Можно поменять порт:

```bash
PORT=8090 python3 web_app.py
```

Для Railway сервис слушает `0.0.0.0` и берет порт из `PORT`, поэтому дополнительная настройка bind host не нужна.

## Upload reference files

В блоке `Reference inputs` можно загрузить:

- image files для `Image URLs`
- first frame image
- last frame image
- video refs
- audio refs

Файлы сохраняются в `uploads/` и доступны как:

```text
https://your-railway-domain.up.railway.app/uploads/<file>
```

Лимит размера по умолчанию: 50 MB на файл. Можно изменить:

```env
MAX_UPLOAD_BYTES=104857600
```

## API сервиса

`POST /api/generate`

```json
{
  "provider": "seedanceapi",
  "prompt": "A cinematic aerial shot over coastline at golden hour",
  "model": "seedance-2.0",
  "duration": 10,
  "aspectRatio": "16:9"
}
```

`GET /api/status?provider=seedanceapi&taskId=...`

Ответы нормализуются до:

```json
{
  "taskId": "task-id",
  "status": "SUCCESS",
  "videoUrls": ["https://...mp4"],
  "lastFrameUrl": null,
  "error": null,
  "raw": {}
}
```

## Провайдеры

`byteplus`

- Base URL: `https://ark.ap-southeast.bytepluses.com/api/v3`
- Submit: `POST /contents/generations/tasks`
- Status: `GET /contents/generations/tasks/{task_id}`
- Model: `dreamina-seedance-2-0-260128`
- Token env: `ARK_API_KEY`, `BYTEPLUS_ARK_API_KEY`, `BYTEPLUS_API_KEY`, `SEEDANCE_API_KEY`

`seedanceapi`

- Base URL: `https://seedanceapi.org`
- Submit: `POST /v2/generate`
- Status: `GET /v2/status?task_id=...`
- Models: `seedance-2.0`, `seedance-2.0-fast`

`reapi`

- Base URL: `https://reapi.ai`
- Submit: `POST /api/v1/videos/generations`
- Status: `GET /api/v1/tasks/{task_id}`
- Models: `doubao-seedance-2.0`, `doubao-seedance-2.0-fast`, `doubao-seedance-2.0-face`, `doubao-seedance-2.0-fast-face`
