#!/usr/bin/env bash
# ============================================================
# LizardBot — инсталлятор для Ubuntu 22.04+
# Использование: sudo bash install.sh [--dir /opt/lizardbot] [--user lizardbot] [--no-service]
# ============================================================
set -euo pipefail

# ── Параметры по умолчанию ───────────────────────────────────
INSTALL_DIR="/opt/lizardbot"
BOT_USER="lizardbot"
SETUP_SERVICE=true
PYTHON_BIN=""            # будет определён автоматически
REQUIRED_PYTHON="3.11"

# ── Цвета ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Аргументы ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --dir)        INSTALL_DIR="$2"; shift 2 ;;
    --user)       BOT_USER="$2";    shift 2 ;;
    --no-service) SETUP_SERVICE=false; shift ;;
    *) error "Неизвестный аргумент: $1" ;;
  esac
done

# ── Проверка root ─────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Запустите скрипт с sudo: sudo bash install.sh"

# ── 1. Системные зависимости ──────────────────────────────────
info "Установка системных зависимостей..."
apt-get update -qq
apt-get install -y -qq \
  software-properties-common git openssl \
  python3.11 python3.11-venv python3.11-dev \
  libffi-dev libssl-dev build-essential

# ── 2. Выбор Python ───────────────────────────────────────────
for candidate in python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    major=${ver%%.*}; minor=${ver##*.}
    if [[ "$major" -eq 3 && "$minor" -ge 10 && "$minor" -ne 12 ]]; then
      PYTHON_BIN="$candidate"
      info "Используем Python $ver ($candidate)"
      break
    elif [[ "$minor" -eq 12 ]]; then
      warn "Python 3.12 обнаружен, но имеет известные проблемы с C-расширениями — пропускаем"
    fi
  fi
done

[[ -n "$PYTHON_BIN" ]] || error "Python 3.11 не найден. Установите: sudo apt install python3.11"

# ── 3. Пользователь ───────────────────────────────────────────
if ! id "$BOT_USER" &>/dev/null; then
  info "Создание пользователя $BOT_USER..."
  useradd -r -s /bin/bash -m -d "/home/$BOT_USER" "$BOT_USER"
else
  info "Пользователь $BOT_USER уже существует"
fi

# ── 4. Директория установки ───────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Обновление репозитория в $INSTALL_DIR..."
  sudo -u "$BOT_USER" git -C "$INSTALL_DIR" pull
else
  info "Копирование файлов в $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  # Копируем из директории скрипта (если запущен из репозитория)
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"
  fi
fi
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR"

# ── 5. Виртуальное окружение ──────────────────────────────────
VENV_DIR="$INSTALL_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
  info "Обновление venv..."
else
  info "Создание venv на $PYTHON_BIN..."
  sudo -u "$BOT_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

info "Установка Python-зависимостей..."
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -q --upgrade pip
sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# ── 6. SSL-сертификат (самоподписной, если нет) ───────────────
CERTS_DIR="$INSTALL_DIR/certs"
if [[ ! -f "$CERTS_DIR/cert.pem" ]]; then
  info "Генерация самоподписного SSL-сертификата..."
  mkdir -p "$CERTS_DIR"
  SERVER_IP=$(hostname -I | awk '{print $1}')
  openssl req -x509 -newkey rsa:4096 \
    -keyout "$CERTS_DIR/key.pem" \
    -out    "$CERTS_DIR/cert.pem" \
    -days 365 -nodes \
    -subj "/C=RU/O=LizardBot/CN=$SERVER_IP" \
    -addext "subjectAltName=IP:${SERVER_IP},IP:127.0.0.1,DNS:localhost" \
    2>/dev/null
  chmod 600 "$CERTS_DIR/key.pem"
  chown -R "$BOT_USER:$BOT_USER" "$CERTS_DIR"
  info "Сертификат: $CERTS_DIR/cert.pem (CN=$SERVER_IP)"
else
  info "SSL-сертификат уже существует, пропускаем"
fi

# ── 7. Конфиг ─────────────────────────────────────────────────
CONFIG="$INSTALL_DIR/config.json"
if [[ ! -f "$CONFIG" ]]; then
  info "Создание config.json из шаблона..."
  cp "$INSTALL_DIR/config.example.json" "$CONFIG"
  chown "$BOT_USER:$BOT_USER" "$CONFIG"
  warn "config.json создан из шаблона. Отредактируйте его перед запуском:"
  warn "  $CONFIG"
else
  info "config.json уже существует, пропускаем"
fi

# ── 8. Рабочие директории ─────────────────────────────────────
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

# ── 9. logrotate ──────────────────────────────────────────────
cat > /etc/logrotate.d/lizardbot <<EOF
$INSTALL_DIR/logs/bot.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
info "logrotate настроен"

# ── 10. systemd-сервис ────────────────────────────────────────
if [[ "$SETUP_SERVICE" == true ]]; then
  SERVICE_FILE="/etc/systemd/system/lizardbot.service"
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LizardBot Trading Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
Group=$BOT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 -m src.main
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=60
StartLimitBurst=3
StandardOutput=append:$INSTALL_DIR/logs/bot.log
StandardError=append:$INSTALL_DIR/logs/bot.log
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable lizardbot
  info "systemd-сервис зарегистрирован и включён в автозапуск"
fi

# ── Итог ──────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo -e "${GREEN}  LizardBot успешно установлен!             ${NC}"
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo ""
echo "  Директория:  $INSTALL_DIR"
echo "  Пользователь: $BOT_USER"
echo "  Python:       $("$PYTHON_BIN" --version)"
echo ""
if [[ ! -s "$CONFIG" ]] || grep -q "YOUR_POLYGON" "$CONFIG" 2>/dev/null; then
  echo -e "  ${YELLOW}⚠  Заполните конфиг перед запуском:${NC}"
  echo "     nano $CONFIG"
  echo ""
fi
if [[ "$SETUP_SERVICE" == true ]]; then
  echo "  Запуск:     sudo systemctl start lizardbot"
  echo "  Статус:     sudo systemctl status lizardbot"
  echo "  Логи:       journalctl -u lizardbot -f"
else
  echo "  Запуск:     sudo -u $BOT_USER $VENV_DIR/bin/python3 -m src.main"
fi
echo ""
echo "  Веб-интерфейс: https://$(hostname -I | awk '{print $1}'):8443"
echo ""
