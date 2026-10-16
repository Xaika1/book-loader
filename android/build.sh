#!/usr/bin/env bash
# Собирает приложение «Учебники» в ../apk/uchebniki.apk без Gradle.
# Нужно: JDK 17+ и Android SDK (build-tools, platforms;android-36).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
JAR="$SDK/platforms/android-36/android.jar"
if [ -z "${JAVA_HOME:-}" ] && command -v mise >/dev/null; then JAVA_HOME="$(mise where java@temurin-21 2>/dev/null || true)"; fi
[ -n "${JAVA_HOME:-}" ] && export JAVA_HOME PATH="$JAVA_HOME/bin:$PATH"
JAVAC=javac
KEYTOOL=keytool
[ -f "$JAR" ] || { echo "Нет $JAR — установите platforms;android-36"; exit 1; }

KEYSTORE=release.keystore
KS_PASS=uchebniki
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

if [ ! -f "$KEYSTORE" ]; then
  echo "Создаю ключ подписи $KEYSTORE (храните его: без него не выйдет обновить приложение на планшетах)"
  "$KEYTOOL" -genkeypair -keystore "$KEYSTORE" -alias uchebniki -keyalg RSA -keysize 2048 -validity 36500 \
    -storepass "$KS_PASS" -keypass "$KS_PASS" -dname "CN=Uchebniki" >/dev/null
fi

"$BT/aapt2" compile --dir res -o "$OUT/res.zip"
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml --java "$OUT/gen" -o "$OUT/base.apk" "$OUT/res.zip"
"$JAVAC" -source 8 -target 8 -Xlint:-options -encoding UTF-8 -bootclasspath "$JAR:$BT/core-lambda-stubs.jar" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')
"$BT/d8" --release --min-api 21 --lib "$JAR" --output "$OUT/dex" $(find "$OUT/classes" -name '*.class')

cp "$OUT/base.apk" "$OUT/unsigned.apk"
python3 - "$OUT/unsigned.apk" "$OUT/dex/classes.dex" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "a", zipfile.ZIP_DEFLATED) as z:
    z.write(sys.argv[2], "classes.dex")
PY
"$BT/zipalign" -p -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
mkdir -p ../apk
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass "pass:$KS_PASS" --out ../apk/uchebniki.apk "$OUT/aligned.apk"
rm -f ../apk/uchebniki.apk.idsig
echo "Готово: $(readlink -f ../apk/uchebniki.apk) ($(du -h ../apk/uchebniki.apk | cut -f1))"
