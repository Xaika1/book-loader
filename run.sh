#!/usr/bin/env bash
# Запуск программы «Учебники по Wi-Fi»
cd "$(dirname "$(readlink -f "$0")")"

if ! command -v python3 >/dev/null; then
  echo "Не найден Python 3. Установите его (Arch: sudo pacman -S python; Ubuntu: sudo apt install python3)"
  read -rp "Нажмите Enter, чтобы закрыть"; exit 1
fi

# Файрвол не пустит планшеты к программе, пока не открыты порты
if command -v ufw >/dev/null && systemctl is-active --quiet ufw 2>/dev/null; then
  echo "Включён файрвол ufw. Если планшеты не находят компьютер, один раз выполните:"
  echo "  sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp"
  echo
elif command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld 2>/dev/null; then
  echo "Включён файрвол firewalld. Если планшеты не находят компьютер, один раз выполните:"
  echo "  sudo firewall-cmd --permanent --add-port=8765/tcp --add-port=8766/udp && sudo firewall-cmd --reload"
  echo
fi
exec python3 server.py "$@"
