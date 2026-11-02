FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# ffmpeg нужен для разбора видео-инструкций по возвратам; xvfb, freerdp, xdotool, imagemagick для удалённого терминала RDP
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg xvfb xdotool freerdp2-x11 imagemagick && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузер для входа в кабинеты поставщиков (Chromium со всеми системными библиотеками)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/*

COPY app ./app
RUN useradd --no-create-home --uid 10001 appuser \
    && mkdir -p /data/files && chown appuser /data/files
USER appuser

EXPOSE 8000
# Один процесс: планировщик проверок интеграций не должен запускаться дважды.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
