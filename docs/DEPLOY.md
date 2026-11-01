# Развёртывание на сервере

Для разработчиков. Один раз настраиваете сервер, дальше система обновляется сама при каждом изменении в ветке `main`, если тесты прошли.

## Что нужно

- Linux-сервер (Ubuntu 22.04+), 2 ядра, 4 ГБ памяти, 40 ГБ диска.
- Docker и Docker Compose плагин (`docker compose version`).
- Домен, направленный на сервер, и порты 80 и 443.
- Пользователь `operdir` в группе `docker`.

## Разовая настройка сервера

```bash
sudo mkdir -p /opt/operdir && sudo chown operdir: /opt/operdir
sudo -u operdir git clone https://github.com/bonya242424/OperDirCLODEmirzap.git /opt/operdir
cd /opt/operdir
cp .env.example .env
mkdir -p secrets
```

Заполните `.env`:

| Переменная | Что это |
|---|---|
| `SECRET_KEY` | `openssl rand -hex 32`. Без него в боевом режиме система не стартует |
| `POSTGRES_PASSWORD` | длинный пароль базы |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | первый владелец, создаётся при первом запуске |
| `HTTP_PORT` | `8080`, если перед сайтом стоит HTTPS-прокси из следующего раздела |
| остальное | ключи интеграций добавляются по мере подключения |

Первый запуск:

```bash
docker compose up -d --build
curl -s http://localhost:${HTTP_PORT:-80}/api/health   # {"status":"ok"}
```

## HTTPS

Без HTTPS пароли идут по сети открытым текстом. Самый простой способ, Caddy на хосте:

```
# /etc/caddy/Caddyfile
operdir.example.com {
    reverse_proxy localhost:8080
}
```

Сертификат Caddy получает и продлевает сам.

## Автоматическая выкладка

1. Создайте ключ только для выкладки: `ssh-keygen -t ed25519 -f deploy_key -N ""`. Публичную часть добавьте в `~operdir/.ssh/authorized_keys`.
2. В репозитории на GitHub: Settings → Secrets and variables → Actions.

   Секреты:
   - `DEPLOY_HOST` — адрес сервера
   - `DEPLOY_USER` — `operdir`
   - `DEPLOY_PATH` — `/opt/operdir`
   - `DEPLOY_SSH_KEY` — содержимое файла `deploy_key` (приватная часть)
   - `DEPLOY_KNOWN_HOSTS` — вывод `ssh-keyscan -p 22 ваш-сервер`
   - `DEPLOY_PORT` — только если порт SSH не 22

   Переменная (вкладка Variables): `DEPLOY_ENABLED` = `true`.
3. Проверьте вручную: Actions → Deploy → Run workflow.

Дальше каждый push в `main` запускает тесты (workflow CI) и, если они прошли, выкладку (workflow Deploy). Скрипт `scripts/deploy.sh` ждёт, пока система ответит; если нет, возвращает прошлую версию и завершается с ошибкой, её видно в Actions.

## Резервные копии

```bash
crontab -e
15 3 * * * /opt/operdir/scripts/backup.sh /var/backups/operdir
```

Копируйте папку `/var/backups/operdir` на другой сервер или в облако: копия на том же диске не спасёт при его потере. Восстановление базы:

```bash
gunzip -c db-XXXX.sql.gz | docker compose exec -T db psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```

## Обновление вручную и откат

```bash
cd /opt/operdir && ./scripts/deploy.sh          # обновить
git reset --hard <версия> && docker compose up -d --build   # откатить на нужный коммит
```

## Важно

- Бэкенд запускается одним процессом: планировщик проверок интеграций не должен работать в двух копиях. Не увеличивайте число процессов и реплик.
- Файлы и база лежат в томах Docker (`files`, `pgdata`). Команда `docker compose down -v` удалит их, не используйте `-v`.
- Файл `.env` и папка `secrets/` не попадают в репозиторий, храните их копию отдельно.
