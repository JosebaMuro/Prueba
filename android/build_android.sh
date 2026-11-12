#!/usr/bin/env bash
# Compila y firma StudioIA-Android.apk sin Android Studio ni SDK completo.
# Herramientas (se descargan solas de Maven Central): apktool (aapt2 + framework), dalvik-dx, apksig,
# y android-all de Robolectric como API de compilación. Requisitos: JDK 17+, python3, curl.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD; TOOLS=${TOOLS:-$HERE/.tools}; OUT=${OUT:-$HERE/build}; DIST=${DIST:-$HERE/../dist}
VERSION_CODE=4; VERSION_NAME=1.2.1; MIN_SDK=26; TARGET_SDK=34
M=https://repo1.maven.org/maven2
mkdir -p "$TOOLS" "$DIST"; rm -rf "$OUT"; mkdir -p "$OUT"/{compiled,gen,classes}

get() { [ -s "$TOOLS/$1" ] && return; for i in 1 2 3 4 5 6; do curl -fsSL -o "$TOOLS/$1" "$2" && return; sleep $((i*5)); done; echo "No se pudo descargar $2"; exit 1; }
get apktool.jar     $M/org/apktool/apktool-cli/3.0.3/apktool-cli-3.0.3.jar
get dx.jar          $M/com/jakewharton/android/repackaged/dalvik-dx/16.0.1/dalvik-dx-16.0.1.jar
get apksig.jar      $M/com/android/tools/build/apksig/2.3.0/apksig-2.3.0.jar
get android-all.jar $M/org/robolectric/android-all/14-robolectric-10818077/android-all-14-robolectric-10818077.jar
case "$(uname -s)" in Darwin) AAPT=macosx/aapt2;; *) AAPT=linux/aapt2;; esac
[ -x "$TOOLS/aapt2" ] || { unzip -o -q -j "$TOOLS/apktool.jar" "prebuilt/$AAPT" -d "$TOOLS"; chmod +x "$TOOLS/aapt2"; }
[ -s "$TOOLS/framework.jar" ] || unzip -p "$TOOLS/apktool.jar" prebuilt/android-framework.jar > "$TOOLS/framework.jar"

echo "== Interfaz y motor"
rm -rf "$OUT/assets" && mkdir -p "$OUT/assets/www"
cp ../frontend/* "$OUT/assets/www/" && cp assets/www/local.js "$OUT/assets/www/"

echo "== Recursos (aapt2)"
"$TOOLS/aapt2" compile --dir res -o "$OUT/compiled/res.zip"
"$TOOLS/aapt2" link -I "$TOOLS/framework.jar" --manifest AndroidManifest.xml "$OUT/compiled/res.zip" \
  --java "$OUT/gen" --min-sdk-version $MIN_SDK --target-sdk-version $TARGET_SDK \
  --version-code $VERSION_CODE --version-name $VERSION_NAME -A "$OUT/assets" -o "$OUT/base.apk"

echo "== Código (javac + dx)"
javac -nowarn -source 8 -target 8 -encoding UTF-8 -cp "$TOOLS/android-all.jar" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java') 2>&1 | grep -v "^warning\|^Note\|^1 warning\|bootstrap" || true
[ -f "$OUT/classes/com/studioia/local/MainActivity.class" ] || { echo "Fallo de compilación"; exit 1; }
java -cp "$TOOLS/dx.jar" com.android.dx.command.Main --dex --min-sdk-version=$MIN_SDK --output="$OUT/classes.dex" "$OUT/classes"
(cd "$OUT" && cp base.apk unsigned.apk && zip -q -j unsigned.apk classes.dex)
# Motor llama.cpp para el modo autónomo (lo genera build_engine.sh). Sin él, la APK solo funciona conectada al PC.
if [ -f jniLibs/arm64-v8a/libllama-server.so ]; then
  mkdir -p "$OUT/lib/arm64-v8a" && cp jniLibs/arm64-v8a/libllama-server.so "$OUT/lib/arm64-v8a/"
  (cd "$OUT" && zip -q -r unsigned.apk lib) && echo "   motor incluido"
else
  echo "   AVISO: falta jniLibs/arm64-v8a/libllama-server.so (ejecuta build_engine.sh); APK sin motor"
fi

echo "== Alineado y firma"
KS=${KEYSTORE:-$HERE/release.p12}; PASS=${KEYSTORE_PASS:-studioia}
[ -f "$KS" ] || keytool -genkeypair -storetype PKCS12 -keystore "$KS" -storepass "$PASS" -alias studioia \
  -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=Studio IA Local, O=Studio IA Local" 2>/dev/null
# Firma v1 + v2 propia (tools/sign.py, necesita "pip install cryptography") y verificación con apksig
python3 tools/sign.py "$KS" "$PASS" "$OUT/unsigned.apk" "$OUT/signed.apk"
javac -nowarn -cp "$TOOLS/apksig.jar" -d "$OUT" tools/Verify.java
java -cp "$OUT:$TOOLS/apksig.jar" Verify "$OUT/signed.apk"
cp "$OUT/signed.apk" "$DIST/StudioIA-Android.apk"
ls -lh "$DIST/StudioIA-Android.apk"
