FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --no-create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# Один процесс: планировщик проверок интеграций не должен запускаться дважды.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
