# Studio IA Local

Estudio de IA **100% local**, **multimodal** (texto, visión, voz e imagen), **multi-agente** (supervisor + especialistas con herramientas), con **memoria persistente** y **workspace** de archivos. Construido sobre Ollama, Qdrant, faster-whisper, Piper y ComfyUI.

| Entregable | Dónde |
|---|---|
| Windows (portátil, sin instalación) | `StudioIA-Windows-x64.zip` en [Releases](../../releases) → descomprimir y abrir `StudioIA.exe` |
| Android 8+ | `StudioIA-Android.apk` en [Releases](../../releases) |
| Linux / servidor | `docker compose up -d` |
| Manuales | [`docs/pdf/`](docs/pdf): `Manual-Windows.pdf`, `Manual-Android.pdf`, `Manual-Tecnico.pdf` |

Inicio rápido en Windows: descomprime el ZIP, abre `StudioIA.exe`, acepta instalar Ollama si te lo pide y pulsa «Descargar los que faltan» en la pestaña Modelos.

Detalles de arquitectura, compilación y API en [`docs/Manual-Tecnico.md`](docs/Manual-Tecnico.md).

## Compilar desde el código

- Windows: `bash build/build_windows.sh` → `dist/StudioIA-Windows-x64.zip`
- Motor Android (llama.cpp): `bash android/build_engine.sh` → `android/jniLibs/arm64-v8a/libllama-server.so` (no se versiona; también va adjunto a la Release)
- APK: `bash android/build_android.sh` → `dist/StudioIA-Android.apk`

La clave de firma del APK (`android/release.p12`) **no** está en el repositorio. Si no existe, el script crea una nueva; para publicar actualizaciones compatibles hay que firmar con la clave original (`KEYSTORE=/ruta/release.p12 KEYSTORE_PASS=...`).
