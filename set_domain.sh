#!/usr/bin/env bash
# Подключить свой домен и https: bash set_domain.sh dastyor.uz
# Перед этим у регистратора домена должны быть A-записи @ и www на IP сервера,
# а в Oracle Cloud (Security List) открыт входящий порт 443.
set -euo pipefail
cd "$(dirname "$0")"
DOMAIN="${1:?Укажите домен, например: bash set_domain.sh dastyor.uz}"
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN#www.}"; DOMAIN="${DOMAIN%/}"

sed -i '/^DOMAIN=/d;/^SITE_ADDRESS=/d' .env
echo "DOMAIN=$DOMAIN" >> .env
echo "SITE_ADDRESS=\"$DOMAIN, www.$DOMAIN\"" >> .env
bash install.sh
echo "Сайт будет доступен по адресу https://$DOMAIN (сертификат появится за минуту)."
