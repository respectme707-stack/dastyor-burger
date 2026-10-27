#!/usr/bin/env bash
# Один раз: чтобы сервер мог скачивать обновления из закрытого (private) репозитория.
# Создаёт ключ только для чтения и показывает его. Его нужно добавить в GitHub:
# репозиторий → Settings → Deploy keys → Add deploy key (галочку «write» НЕ ставить).
set -euo pipefail
cd "$(dirname "$0")"
REPO=respectme707-stack/dastyor-burger
KEY="$HOME/.ssh/dastyor_deploy"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -N "" -C "dastyor-server" -f "$KEY" >/dev/null
fi
if ! grep -q "dastyor_deploy" "$HOME/.ssh/config" 2>/dev/null; then
  cat >> "$HOME/.ssh/config" <<CFG
Host github.com
  IdentityFile $KEY
  IdentitiesOnly yes
CFG
  chmod 600 "$HOME/.ssh/config"
fi
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
git remote set-url origin "git@github.com:$REPO.git"
echo
echo "Скопируйте строку ниже и добавьте её в GitHub:"
echo "https://github.com/$REPO/settings/keys/new"
echo
cat "$KEY.pub"
echo
echo "После этого обновлять так: bash ~/dastyor-burger/update.sh"
