#!/usr/bin/env bash
# Создаёт значок «Учебники по Wi-Fi» в меню приложений
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/uchebniki-wifi.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Учебники по Wi-Fi
Comment=Раздача учебников на планшеты по Wi-Fi
Exec="$DIR/run.sh"
Icon=accessories-ebook-reader
Terminal=true
Categories=Education;Utility;
DESKTOP
chmod +x "$DIR/run.sh"
if [ -d "$HOME/Desktop" ]; then cp "$APPS/uchebniki-wifi.desktop" "$HOME/Desktop/" && chmod +x "$HOME/Desktop/uchebniki-wifi.desktop"; fi
if [ -d "$HOME/Рабочий стол" ]; then cp "$APPS/uchebniki-wifi.desktop" "$HOME/Рабочий стол/" && chmod +x "$HOME/Рабочий стол/uchebniki-wifi.desktop"; fi
echo "Готово: значок «Учебники по Wi-Fi» добавлен в меню приложений (и на рабочий стол, если он есть)."
