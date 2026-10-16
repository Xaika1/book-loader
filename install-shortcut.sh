#!/usr/bin/env bash
# Создаёт значок «Книги на планшеты» в меню приложений
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/knigi-na-planshety.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Книги на планшеты
Comment=Загрузка книг на все планшеты сразу
Exec="$DIR/run.sh"
Icon=accessories-ebook-reader
Terminal=true
Categories=Education;Utility;
DESKTOP
chmod +x "$DIR/run.sh"
if [ -d "$HOME/Desktop" ]; then cp "$APPS/knigi-na-planshety.desktop" "$HOME/Desktop/" && chmod +x "$HOME/Desktop/knigi-na-planshety.desktop"; fi
if [ -d "$HOME/Рабочий стол" ]; then cp "$APPS/knigi-na-planshety.desktop" "$HOME/Рабочий стол/" && chmod +x "$HOME/Рабочий стол/knigi-na-planshety.desktop"; fi
echo "Готово: значок «Книги на планшеты» добавлен в меню приложений (и на рабочий стол, если он есть)."
