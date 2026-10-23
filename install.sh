#!/usr/bin/env bash
# Установка на чистый сервер Ubuntu (например, Oracle Cloud Always Free).
# Запуск: bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "== Ставлю Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi

# В образах Ubuntu от Oracle входящие порты закрыты iptables, открываем 80.
if sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null; then
  :
elif sudo iptables -L INPUT -n | grep -q REJECT; then
  echo "== Открываю порт 80"
  sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save >/dev/null 2>&1 || true
fi

IP=$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
FIRST_RUN=0
if [ ! -f .env ]; then
  FIRST_RUN=1
  echo "== Настройка"
  read -rp "Токен бота от @BotFather (можно пропустить, нажав Enter): " TOKEN < /dev/tty
  read -rp "Имя бота без @ [Dastyorburger_bot]: " BOTNAME < /dev/tty
  read -rp "Ваш Telegram ID (узнать у @userinfobot): " OWNER < /dev/tty
  cat > .env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
DB_PASSWORD=$(openssl rand -hex 16)
ALLOWED_HOSTS=*
CSRF_TRUSTED_ORIGINS=http://$IP
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_BOT_USERNAME=${BOTNAME:-Dastyorburger_bot}
OWNER_TELEGRAM_IDS=$OWNER
EOF
  chmod 600 .env
fi

PROFILE=()
if grep -q '^TELEGRAM_BOT_TOKEN=.' .env; then
  PROFILE=(--profile bot)
fi

echo "== Собираю и запускаю (первый раз занимает несколько минут)"
sudo docker compose "${PROFILE[@]}" up -d --build

echo "== Жду базу данных"
until sudo docker compose exec -T web python manage.py migrate --check >/dev/null 2>&1; do
  sleep 3
done

if [ "$FIRST_RUN" = 1 ]; then
  echo "== Создайте логин и пароль владельца для входа на сайт"
  sudo docker compose exec web python manage.py createsuperuser
fi

echo
echo "Готово! Откройте в браузере: http://$IP"
