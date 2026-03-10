# LizardBot — Развёртывание на сервере

## Требования

- Python 3.10+
- Linux (Ubuntu 22.04+)
- OpenSSL (для генерации сертификатов)

---

## 0. Подготовка системы (Ubuntu)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git openssl
```

Создайте системного пользователя (если ещё не существует):

```bash
sudo useradd -r -s /bin/bash -m -d /home/master master
```

Создайте рабочую директорию:

```bash
sudo mkdir -p /opt/lizardbot
sudo chown master:master /opt/lizardbot
```

---

## 1. Клонирование и окружение

```bash
git clone <repo_url> /opt/lizardbot
cd /opt/lizardbot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. SSL-сертификаты

Бот требует HTTPS. Возможны два варианта:

### Вариант A — самоподписной сертификат (для тестирования)

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/key.pem \
  -out certs/cert.pem \
  -days 365 -nodes \
  -subj "/C=RU/O=LizardBot/CN=localhost" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
chmod 600 certs/key.pem
```

Браузер покажет предупреждение о самоподписном сертификате — это нормально для внутреннего использования.

### Вариант B — сертификат Let's Encrypt (для продакшена с доменом)

```bash
sudo apt install certbot
sudo certbot certonly --standalone -d your.domain.com
# Сертификаты будут в /etc/letsencrypt/live/your.domain.com/
```

В `config.json` укажите:
```json
"ssl_certfile": "/etc/letsencrypt/live/your.domain.com/fullchain.pem",
"ssl_keyfile": "/etc/letsencrypt/live/your.domain.com/privkey.pem"
```

---

## 3. Конфигурация

```bash
cp config.example.json config.json
```

Заполните `config.json`:

| Поле | Описание |
|---|---|
| `private_key` | Приватный ключ Polygon-кошелька (только для реального режима) |
| `funder_address` | Адрес кошелька (только для реального режима) |
| `api_key / api_secret / api_passphrase` | CLOB API credentials (опционально, деривируются из ключа) |
| `simulation_mode` | `true` — симуляция без реальных ордеров и без ключей |
| `active` | `false` — стратегия не торгует до ручного включения через UI |
| `server.port` | Порт веб-интерфейса (по умолчанию `8443`) |
| `server.ssl_certfile` | Путь к PEM-сертификату |
| `server.ssl_keyfile` | Путь к приватному ключу PEM |
| `users` | bcrypt-хэши паролей для входа в UI |

### Генерация bcrypt-хэша для нового пользователя

```python
import bcrypt
print(bcrypt.hashpw(b"your_password", bcrypt.gensalt()).decode())
```

Вставьте результат в `config.json`:
```json
"users": {
  "admin": "$2b$12$..."
}
```

---

## 4. Структура директорий

```
LizardBot/
├── config.json          # Основной конфиг (создать из config.example.json)
├── certs/               # SSL-сертификаты (в .gitignore — не коммитить)
│   ├── cert.pem
│   └── key.pem
├── data/                # SQLite-база (создаётся автоматически)
├── logs/                # Лог-файлы (создаётся автоматически)
├── frontend/            # Статика веб-UI (раздаётся встроенным сервером)
└── src/                 # Исходный код
```

Директории `logs/` и `data/` создаются автоматически. Внешняя СУБД не требуется — используется встроенный SQLite.

---

## 5. Запуск вручную (тест)

```bash
cd /opt/lizardbot
source .venv/bin/activate
python3 -m src.main
```

Интерфейс доступен по адресу: `https://<server_ip>:8443`
Логин: `admin` / пароль: `asdfg321!Q` (из `config.example.json`)

---

## 6. systemd-сервис (автозапуск)

Создайте файл `/etc/systemd/system/lizardbot.service`:

```ini
[Unit]
Description=LizardBot Trading Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=master
Group=master
WorkingDirectory=/opt/lizardbot
ExecStart=/opt/lizardbot/.venv/bin/python3 -m src.main
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=60
StartLimitBurst=3

# Логи пишутся в файл (дополнительно доступны через journalctl)
StandardOutput=append:/opt/lizardbot/logs/bot.log
StandardError=append:/opt/lizardbot/logs/bot.log

# Ограничения безопасности
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Активируйте и запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lizardbot    # Автозапуск при старте системы
sudo systemctl start lizardbot
sudo systemctl status lizardbot
```

### Управление сервисом

```bash
sudo systemctl stop lizardbot      # Остановить
sudo systemctl restart lizardbot   # Перезапустить
sudo systemctl disable lizardbot   # Отключить автозапуск
journalctl -u lizardbot -f         # Логи в реальном времени
journalctl -u lizardbot --since "1 hour ago"  # Логи за последний час
```

### Диагностика

```bash
# Статус и последние ошибки
sudo systemctl status lizardbot

# Бот упал и не поднимается — посмотреть причину
journalctl -u lizardbot -n 50 --no-pager

# Проверить занятость порта
ss -tlnp | grep 8443
fuser 8443/tcp

# Проверить что бот слушает
curl -k https://localhost:8443/login
```

---

## 7. Брандмауэр (UFW)

Если UFW активен, откройте порт бота:

```bash
sudo ufw allow 8443/tcp comment "LizardBot UI"
sudo ufw status
```

Если используется Nginx (порт 443):

```bash
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp
sudo ufw deny 8443/tcp  # закрыть прямой доступ, оставить только через Nginx
```

---

## 8. Ротация логов

Создайте файл `/etc/logrotate.d/lizardbot`:

```
/opt/lizardbot/logs/bot.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Проверка:

```bash
sudo logrotate -d /etc/logrotate.d/lizardbot   # тест без применения
sudo logrotate -f /etc/logrotate.d/lizardbot   # принудительная ротация
```

---

## 9. Nginx reverse proxy (опционально)

Если нужен порт 443 вместо 8443, или дополнительная балансировка:

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;

    ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;

    location / {
        proxy_pass https://127.0.0.1:8443;
        proxy_http_version 1.1;

        # WebSocket
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}

server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}
```

> **Важно:** заголовки `Upgrade` и `Connection` обязательны для работы WebSocket (`/ws`).

---

## 10. Обновление бота

```bash
cd /opt/lizardbot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart lizardbot
```

---

## 11. Работа в режиме симуляции

В `config.json` установите:

```json
"simulation_mode": true,
"private_key": "",
"funder_address": ""
```

В симуляции:
- Gamma API (поиск рынков, вероятности) работает полностью
- Реальные ордера не размещаются
- Баланс показывается как $100
- Ключи Polygon не нужны

Для включения/отключения торговли используйте кнопки **Start / Stop** в веб-интерфейсе.
