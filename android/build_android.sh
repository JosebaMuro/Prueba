#!/usr/bin/env bash
# Compila y firma las APK de Studio IA sin Android Studio ni SDK completo:
#   StudioIA-Android.apk            motores llama.cpp, ONNX Runtime GenAI, ExecuTorch (CPU/GPU Vulkan) y LiteRT-LM (CPU/GPU)
#   StudioIA-Android-Snapdragon.apk lo mismo + Qualcomm Genie (GenieX): NPU Hexagon, GPU Adreno y CPU
# Herramientas y bibliotecas (se descargan solas): apktool (aapt2 + framework), D8 de R8, apksig, android-all de
# Robolectric como API de compilación, y los motores desde Maven Central, NuGet y PyPI.
# Requisitos: JDK 17+, python3 (con pip), curl, unzip. VARIANTS="standard" para compilar solo la normal.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD; TOOLS=${TOOLS:-$HERE/.tools}; OUTBASE=${OUT:-$HERE/build}; DIST=${DIST:-$HERE/../dist}
VERSION_CODE=5; VERSION_NAME=1.3.0; MIN_SDK=26; TARGET_SDK=34
VARIANTS=${VARIANTS:-"standard snapdragon"}
M=https://repo1.maven.org/maven2
mkdir -p "$TOOLS" "$DIST"

get() { [ -s "$TOOLS/$1" ] && return; for i in 1 2 3 4 5 6; do curl -fsSL -o "$TOOLS/$1.tmp" "$2" && mv "$TOOLS/$1.tmp" "$TOOLS/$1" && return; sleep $((i*5)); done; echo "No se pudo descargar $2"; exit 1; }
get apktool.jar     $M/org/apktool/apktool-cli/3.0.3/apktool-cli-3.0.3.jar
get r8.jar          https://storage.googleapis.com/r8-releases/raw/8.5.35/r8lib.jar
get apksig.jar      $M/com/android/tools/build/apksig/2.3.0/apksig-2.3.0.jar
get android-all.jar $M/org/robolectric/android-all/14-robolectric-10818077/android-all-14-robolectric-10818077.jar
case "$(uname -s)" in Darwin) AAPT=macosx/aapt2;; *) AAPT=linux/aapt2;; esac
[ -x "$TOOLS/aapt2" ] || { unzip -o -q -j "$TOOLS/apktool.jar" "prebuilt/$AAPT" -d "$TOOLS"; chmod +x "$TOOLS/aapt2"; }
[ -s "$TOOLS/framework.jar" ] || unzip -p "$TOOLS/apktool.jar" prebuilt/android-framework.jar > "$TOOLS/framework.jar"

echo "== Motores (bibliotecas oficiales)"
# ONNX Runtime GenAI: el AAR de Android viaja dentro del paquete NuGet oficial; necesita libonnxruntime.so
get ortgenai.nupkg  https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntimegenai/0.19.0/microsoft.ml.onnxruntimegenai.0.19.0.nupkg
[ -s "$TOOLS/ortgenai.aar" ] || unzip -p "$TOOLS/ortgenai.nupkg" runtimes/android/native/onnxruntime-genai.aar > "$TOOLS/ortgenai.aar"
get ort.aar         $M/com/microsoft/onnxruntime/onnxruntime-android/1.31.1/onnxruntime-android-1.31.1.aar
# ExecuTorch con XNNPACK (CPU) y Vulkan (GPU), y sus dependencias Java
get executorch.aar  $M/org/pytorch/executorch-android-vulkan/1.5.1/executorch-android-vulkan-1.5.1.aar
get fbjni.aar       $M/com/facebook/fbjni/fbjni/0.7.0/fbjni-0.7.0.aar
get nativeloader.jar $M/com/facebook/soloader/nativeloader/0.10.5/nativeloader-0.10.5.jar
get kotlin-stdlib.jar $M/org/jetbrains/kotlin/kotlin-stdlib/2.1.21/kotlin-stdlib-2.1.21.jar
# LiteRT-LM: biblioteca C de Google (rueda de PyPI para Android) + puente JNI propio (jni/litertlm_jni.c)
if [ ! -s "$TOOLS/liblitert-lm.so" ]; then
  python3 -m pip download -q --no-deps --only-binary :all: --platform android_23_arm64_v8a --python-version 3.12 \
    -d "$TOOLS/litert" "litert-lm-api==0.19.1"
  unzip -p "$TOOLS"/litert/litert_lm_api-*.whl litert_lm/liblitert-lm.so > "$TOOLS/liblitert-lm.so"
fi
if [ ! -s "$TOOLS/liblitertlm_jni.so" ] || [ jni/litertlm_jni.c -nt "$TOOLS/liblitertlm_jni.so" ]; then
  [ -d "$TOOLS/zigvenv" ] || python3 -m venv "$TOOLS/zigvenv"
  "$TOOLS/zigvenv/bin/pip" install -q ziglang
  JH=$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")
  ZIG="$TOOLS/zigvenv/bin/python -m ziglang"
  # Sin libc: solo JNI y las funciones de liblitert-lm (no hace falta el NDK)
  $ZIG cc -target aarch64-linux-musl -c -fPIC -O2 -fno-builtin -fno-stack-protector -I"$JH/include" -I"$JH/include/linux" \
    jni/litertlm_jni.c -o "$TOOLS/litertlm_jni.o"
  $ZIG ld.lld -shared "$TOOLS/litertlm_jni.o" -L"$TOOLS" -llitert-lm --allow-shlib-undefined -z max-page-size=16384 \
    -soname liblitertlm_jni.so --hash-style=both -o "$TOOLS/liblitertlm_jni.so"
fi
# Qualcomm Genie para Android (GenieX de Qualcomm) y corrutinas de Kotlin que usa
get geniex.aar      $M/com/qualcomm/qti/geniex-android/0.10.0/geniex-android-0.10.0.aar
get coroutines.jar  $M/org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/1.9.0/kotlinx-coroutines-core-jvm-1.9.0.jar
get coroutines-android.jar $M/org/jetbrains/kotlinx/kotlinx-coroutines-android/1.9.0/kotlinx-coroutines-android-1.9.0.jar
for a in ortgenai ort executorch fbjni geniex; do
  [ -d "$TOOLS/$a" ] || { mkdir -p "$TOOLS/$a" && unzip -q -o "$TOOLS/$a.aar" 'classes.jar' 'jni/arm64-v8a/*' -d "$TOOLS/$a" 2>/dev/null || true; }
done

build() {
  local V=$1 OUT=$OUTBASE/$1 NAME
  NAME=StudioIA-Android.apk; [ "$V" = snapdragon ] && NAME=StudioIA-Android-Snapdragon.apk
  echo; echo "######## Variante: $V → $NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"/{compiled,gen,classes,dex,lib/arm64-v8a}

  echo "== Interfaz"
  mkdir -p "$OUT/assets/www"
  cp ../frontend/* "$OUT/assets/www/" && cp assets/www/*.js "$OUT/assets/www/"
  echo "$V" > "$OUT/assets/www/variant.txt"

  echo "== Recursos (aapt2)"
  "$TOOLS/aapt2" compile --dir res -o "$OUT/compiled/res.zip"
  "$TOOLS/aapt2" link -I "$TOOLS/framework.jar" --manifest AndroidManifest.xml "$OUT/compiled/res.zip" \
    --java "$OUT/gen" --min-sdk-version $MIN_SDK --target-sdk-version $TARGET_SDK \
    --version-code $VERSION_CODE --version-name $VERSION_NAME -A "$OUT/assets" -o "$OUT/base.apk"

  echo "== Código (javac + D8)"
  local LIBS="$TOOLS/ortgenai/classes.jar:$TOOLS/executorch/classes.jar:$TOOLS/fbjni/classes.jar:$TOOLS/nativeloader.jar:$TOOLS/kotlin-stdlib.jar"
  local SRCS; SRCS=$(find src "$OUT/gen" -name '*.java')
  local PROGRAM=("$TOOLS/ortgenai/classes.jar" "$TOOLS/executorch/classes.jar" "$TOOLS/fbjni/classes.jar" "$TOOLS/nativeloader.jar" "$TOOLS/kotlin-stdlib.jar")
  if [ "$V" = snapdragon ]; then
    LIBS="$LIBS:$TOOLS/geniex/classes.jar:$TOOLS/coroutines.jar"
    SRCS="$SRCS $(find src-snapdragon -name '*.java')"
    PROGRAM+=("$TOOLS/geniex/classes.jar" "$TOOLS/coroutines.jar" "$TOOLS/coroutines-android.jar")
  fi
  javac -nowarn -source 8 -target 8 -encoding UTF-8 -cp "$TOOLS/android-all.jar:$LIBS" -d "$OUT/classes" $SRCS 2>&1 \
    | grep -v "^warning\|^Note\|^1 warning\|bootstrap\|JAVA_TOOL_OPTIONS" || true
  [ -f "$OUT/classes/com/studioia/local/MainActivity.class" ] || { echo "Fallo de compilación"; exit 1; }
  (cd "$OUT/classes" && jar cf ../app.jar .)
  java -cp "$TOOLS/r8.jar" com.android.tools.r8.D8 --release --min-api $MIN_SDK --lib "$TOOLS/android-all.jar" \
    --output "$OUT/dex" "$OUT/app.jar" "${PROGRAM[@]}" 2>&1 | grep -v "JAVA_TOOL_OPTIONS\|Warning in\|Type .* was not found\|Missing class" || true
  ls "$OUT"/dex/classes*.dex >/dev/null
  (cd "$OUT" && cp base.apk unsigned.apk && zip -q -j unsigned.apk dex/classes*.dex)

  echo "== Motores nativos (arm64-v8a)"
  local L="$OUT/lib/arm64-v8a"
  if [ -f jniLibs/arm64-v8a/libllama-server.so ]; then cp jniLibs/arm64-v8a/libllama-server.so "$L/"
  else echo "   AVISO: falta jniLibs/arm64-v8a/libllama-server.so (ejecuta build_engine.sh); APK sin llama.cpp"; fi
  cp "$TOOLS"/ortgenai/jni/arm64-v8a/*.so "$TOOLS"/ort/jni/arm64-v8a/libonnxruntime.so "$L/"
  cp "$TOOLS"/executorch/jni/arm64-v8a/*.so "$TOOLS"/fbjni/jni/arm64-v8a/*.so "$L/"
  cp "$TOOLS/liblitert-lm.so" "$TOOLS/liblitertlm_jni.so" "$L/"
  [ "$V" = snapdragon ] && cp "$TOOLS"/geniex/jni/arm64-v8a/*.so "$L/"
  rm -f "$L/libonnxruntime4j_jni.so"
  echo "   $(ls "$L" | wc -l) bibliotecas, $(du -sh "$L" | cut -f1)"
  (cd "$OUT" && zip -q -r unsigned.apk lib)

  echo "== Alineado y firma"
  local KS=${KEYSTORE:-$HERE/release.p12} PASS=${KEYSTORE_PASS:-studioia}
  [ -f "$KS" ] || keytool -genkeypair -storetype PKCS12 -keystore "$KS" -storepass "$PASS" -alias studioia \
    -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=Studio IA Local, O=Studio IA Local" 2>/dev/null
  # Firma v1 + v2 propia (tools/sign.py, necesita "pip install cryptography") y verificación con apksig
  python3 tools/sign.py "$KS" "$PASS" "$OUT/unsigned.apk" "$OUT/signed.apk"
  javac -nowarn -cp "$TOOLS/apksig.jar" -d "$OUT" tools/Verify.java
  java -cp "$OUT:$TOOLS/apksig.jar" Verify "$OUT/signed.apk"
  cp "$OUT/signed.apk" "$DIST/$NAME"
  ls -lh "$DIST/$NAME"
}

for v in $VARIANTS; do build "$v"; done
