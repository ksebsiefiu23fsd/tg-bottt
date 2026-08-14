# Скачайка Бот

Локальный Telegram-бот для скачивания видео, поиска музыки, распознавания
композиций и получения текстов песен.

## Возможности

- скачивание видео с YouTube, RuTube, TikTok и Instagram;
- поиск музыки по названию или исполнителю;
- распознавание музыки по ссылке YouTube/TikTok и из MP4;
- выбор одного из 10 вариантов и отправка в MP3;
- получение текста песни через LRCLIB;
- автономный перевод с английского на русский через Argos Translate.

## Требования

- Windows;
- Python 3.11;
- доступ к Telegram API;
- токен Telegram-бота.

Python 3.11 необходим из-за готовой Windows-сборки `shazamio-core`.

## Установка

Откройте PowerShell в папке проекта и выполните:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

Откройте созданный файл `.env` и укажите `BOT_TOKEN`. Затем запустите:

```powershell
.\start.ps1
```

Для остановки нажмите `Ctrl+C`.

## Настройки `.env`

```env
BOT_TOKEN=ADD_TOKEN_HERE
ALLOWED_USER_ID=5618399651
MAX_VIDEO_DURATION=3600
MAX_FILE_SIZE_MB=49
LRCLIB_API_URL=https://lrclib.net/api
ARGOS_SOURCE_LANGUAGE=en
ARGOS_TARGET_LANGUAGE=ru
```

Во время `setup.ps1` языковая модель Argos Translate `en → ru` скачивается и
устанавливается один раз. После установки перевод работает без отдельного
сервера и без подключения к интернету.

## Структура

```text
assets/            изображения бота
bot.py             основной код
requirements.txt   зависимости Python
setup.ps1          создание окружения Python 3.11
start.ps1          запуск бота
.env.example       пример конфигурации
```

Рабочее виртуальное окружение создаётся в `.venv311/`.
