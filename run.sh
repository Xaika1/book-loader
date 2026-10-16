#!/usr/bin/env bash
# Запуск программы «Книги на планшеты»
cd "$(dirname "$(readlink -f "$0")")"

if ! command -v python3 >/dev/null; then
  echo "Не найден Python 3. Установите его (Arch: sudo pacman -S python; Ubuntu: sudo apt install python3)"
  read -rp "Нажмите Enter, чтобы закрыть"; exit 1
fi
if [ -z "$ADB" ] && ! command -v adb >/dev/null; then
  echo "Не найдена программа adb. Установите её:"
  echo "  Arch/Manjaro:  sudo pacman -S android-tools android-udev"
  echo "  Ubuntu/Debian: sudo apt install adb"
  echo "Программа запустится, но планшеты видны не будут."
fi
exec python3 server.py "$@"
