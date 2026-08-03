# Seedance / Seedream Studio

Локальный web-сервис для генерации видео через Seedance 2 и изображений через Seedream 5 compatible BytePlus Ark API.

## Возможности

- Генерация через BytePlus Ark Seedance 2.0, как в Yango Perf.
- Генерация изображений через BytePlus Ark Seedream 5 (`seedream-5-0-260128`).
- Text-to-video через `seedanceapi.org/v2`.
- Поддержка reAPI `doubao-seedance-2.0` variants.
- Elements-style references через `@image1`, `@video1`, `@audio1` where provider supports them.
- Private real-person portrait library через BytePlus Assets API: H5-проверка личности, группы, загрузка и выбор `asset://` references.
- Upload reference image/video/audio files and serve them as public `/uploads/...` URLs.
- Persistent material library with local IDs, review status, Asset ID copy/reuse, and SHA-256 deduplication.
- Polling статуса видео-задачи, предпросмотр готового MP4 и синхронный предпросмотр Seedream images.
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

Для приватной библиотеки портретов нужен отдельный Access Key / Secret Key:

```env
BYTEPLUS_ACCESS_KEY_ID="your_ak"
BYTEPLUS_SECRET_ACCESS_KEY="your_sk"
BYTEPLUS_ASSET_PROJECT="default"
```

Поддерживаются также имена `BYTEPLUS_ACCESS_KEY`, `BYTEPLUS_AK`,
`ARK_ACCESS_KEY_ID`, `BYTEPLUS_SECRET_KEY`, `BYTEPLUS_SK` и
`ARK_SECRET_ACCESS_KEY`.

Если BytePlus выдал отдельный endpoint ID, добавьте его тоже:

```env
SEEDANCE_ENDPOINT_ID="your_endpoint_id"
```

Для Seedream image generation можно отдельно задать image endpoint ID:

```env
SEEDREAM_ENDPOINT_ID="your_image_endpoint_id"
```

Также поддерживаются:

```env
BYTEPLUS_ARK_API_KEY="your_key"
BYTEPLUS_API_KEY="your_key"
SEEDANCE_API_KEY="your_key"
BYTEPLUS_ARK_ENDPOINT_ID="your_endpoint_id"
ARK_ENDPOINT_ID="your_endpoint_id"
BYTEPLUS_SEEDREAM_ENDPOINT_ID="your_image_endpoint_id"
ARK_IMAGE_ENDPOINT_ID="your_image_endpoint_id"
```

Для reAPI:

```env
REAPI_API_KEY="your_key"
```

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

Для постоянного хранения материалов на Railway подключите Volume. Сервис автоматически
использует `RAILWAY_VOLUME_MOUNT_PATH`; локально можно указать каталог вручную:

```env
SEEDANCE_DATA_DIR="/path/to/persistent/data"
```

## Upload reference files

В блоке `Files` можно загрузить изображения, видео и аудио. Изображения можно сортировать drag-and-drop; в prompt их удобно указывать как `@image1`, `@image2`, а ссылки на изображения, вставленные прямо в prompt, автоматически подтягиваются как previews.

Файлы сохраняются в `uploads/`, автоматически добавляются в `material_library.json`
и доступны как:

```text
https://your-railway-domain.up.railway.app/uploads/<file>
```

Лимит размера по умолчанию: 50 MB на файл. Можно изменить:

```env
MAX_UPLOAD_BYTES=104857600
```

## Private portrait assets

Интеграция рассчитана на
[Dreamina Seedance 2.0 Advanced Creation Rights](https://docs.byteplus.com/en/docs/modelark/2333589)
и работает только после активации прав в BytePlus, enterprise verification и
выдачи `ArkFullAccess` для нужного project.

Поток в интерфейсе:

1. Нажмите **Verify a person** и завершите H5-проверку BytePlus.
2. После callback приложение получает `GroupId` и обновляет список portrait groups.
3. Выберите фото/видео и нажмите **Save material**. Это только сохраняет файл в локальную библиотеку.
4. В карточке материала нажмите **Send to check**. Только на этом шаге вызывается BytePlus `CreateAsset`.
5. Приложение опрашивает `GetAsset`, пока статус не станет `Active` или `Failed`.
6. Для обычного URL-reference нажмите **Use file**; для проверенного портрета — **Use asset**.
7. **Copy ID** копирует BytePlus Asset ID. Его можно повторно вставить в поле **Reuse by Asset ID**.
8. В prompt обращайтесь к ассетам по порядку: `image 1`, `video 1`, а не по Asset ID.

Все файлы, загруженные через основной блок **Files**, также попадают в библиотеку материалов.
Повторная загрузка идентичного файла определяется по SHA-256 и переиспользует существующую запись.

Важно:

- Файл для `CreateAsset` должен быть доступен BytePlus по публичному HTTPS URL.
  Поэтому регистрация загруженного файла работает на Railway/публичном домене,
  но не через `localhost`.
- Asset и inference endpoint должны находиться в одном `BYTEPLUS_ASSET_PROJECT`.
- AK/SK никогда не отправляются в браузер: HMAC-подпись выполняется на сервере.
- Маршруты управления ассетами используют серверные credentials. Не публикуйте
  приложение без собственной авторизации или Railway private networking.
- После окончания оплаченных прав BytePlus применяет grace period и затем может
  безвозвратно удалить созданные в оплаченный период assets/groups; храните исходники отдельно.

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

`POST /api/generate-image`

```json
{
  "provider": "byteplus",
  "prompt": "A vibrant editorial portrait, sculptural hat, studio lighting",
  "imageSize": "2K",
  "imageOutputFormat": "png",
  "imageWatermark": false,
  "imageUrls": "https://example.com/reference.png"
}
```

Assets API proxy:

```text
POST /api/assets/verification-session
POST /api/assets/verification-result
POST /api/assets/create
GET  /api/assets?projectName=default&groupId=...
GET  /api/assets/status?projectName=default&assetId=...
GET  /api/materials
POST /api/materials/submit
GET  /api/materials/status?materialId=...
```

Ответ нормализуется до:

```json
{
  "imageUrls": ["https://...png"],
  "images": [{"url": "https://...png", "size": "2048x2048"}],
  "usage": {},
  "raw": {}
}
```

## Провайдеры

`byteplus`

- Base URL: `https://ark.ap-southeast.bytepluses.com/api/v3`
- Submit: `POST /contents/generations/tasks`
- Status: `GET /contents/generations/tasks/{task_id}`
- Image submit: `POST /images/generations`
- Model: `dreamina-seedance-2-0-260128`
- Image model: `seedream-5-0-260128`
- Endpoint env: `SEEDANCE_ENDPOINT_ID`, `BYTEPLUS_ARK_ENDPOINT_ID`, `ARK_ENDPOINT_ID`
- Image endpoint env: `SEEDREAM_ENDPOINT_ID`, `BYTEPLUS_SEEDREAM_ENDPOINT_ID`, `ARK_IMAGE_ENDPOINT_ID`
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
