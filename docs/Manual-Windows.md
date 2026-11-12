# Studio IA Local · Manual para Windows

Studio IA Local es un estudio de inteligencia artificial que funciona entero en tu PC: un equipo de agentes que conversa contigo, lee y escribe archivos, ejecuta código, mira imágenes, escucha tu voz y recuerda lo importante entre sesiones. Ningún dato sale de tu ordenador.

## 1. Requisitos

| | Mínimo | Recomendado |
|---|---|---|
| Sistema | Windows 10 (64 bits) | Windows 11 |
| Memoria RAM | 8 GB | 16 GB o más |
| Tarjeta gráfica | No obligatoria | NVIDIA con 8 GB de VRAM o más (o AMD Radeon compatible con Ollama) |
| Disco libre | 15 GB | 40 GB (los modelos ocupan entre 2 y 10 GB cada uno) |

Hace falta Internet **solo la primera vez**, para descargar Ollama y los modelos. Después todo funciona sin conexión.

## 2. Instalación

1. Descomprime `StudioIA-Windows-x64.zip` en una carpeta fija, por ejemplo `C:\StudioIA`. Evita carpetas sincronizadas con OneDrive.
2. Haz doble clic en **StudioIA.exe**.
   - Si Windows SmartScreen muestra «Windows protegió su PC», pulsa **Más información → Ejecutar de todas formas**. Aparece porque el programa no está firmado con un certificado comercial.
3. Si no tienes **Ollama** (el motor que ejecuta los modelos), el estudio te ofrecerá instalarlo con un clic. Si prefieres hacerlo a mano: descárgalo de <https://ollama.com/download> o ejecuta `winget install Ollama.Ollama`.
4. Se abrirá el navegador en `http://127.0.0.1:8765` con el estudio. La primera vez irá directamente a la pestaña **⚙️ Modelos**.
5. Pulsa **«Descargar los que faltan»**. Cuando todas las insignias estén en verde, el estudio está listo.

La ventana negra que queda abierta es el motor del estudio. **Ciérrala para apagarlo.**

## 3. Elegir modelos según tu equipo

En la pestaña **⚙️ Modelos** verás tres perfiles. Pulsa **📋 config** en el tuyo, abre `studio.env` con el Bloc de notas, pega las líneas (sustituyendo las de los modelos) y reinicia StudioIA.exe. Después pulsa «Descargar los que faltan».

| Perfil | MODEL_CHAT y MODEL_CODE | MODEL_VISION | Descarga |
|---|---|---|---|
| Ligero (8 GB RAM, sin GPU) | qwen3-vl:4b-instruct | (el mismo) | 3,3 GB |
| Equilibrado (16 GB o GPU 8 GB) · **por defecto** | qwen3:8b | qwen3-vl:8b-instruct | 11,4 GB |
| Potente (GPU 24 GB) | qwen3:30b-a3b-instruct-2507-q4_K_M | qwen3-vl:8b-instruct | 25 GB |

`MODEL_EMBED=qwen3-embedding:0.6b` (memoria, 0,6 GB, multilingüe) sirve para todos. En el perfil ligero un único modelo hace de chat, programador y visión: la memoria solo tiene que alojar uno.

**Consejos de rendimiento**

- Con GPU NVIDIA, actualiza el controlador: Ollama la usa automáticamente. En ⚙️ Modelos → «Cargados en memoria» ves qué parte de cada modelo va en la GPU. Si no está al 100 %, el modelo no cabe: baja de perfil o pon `NUM_CTX=4096`.
- La primera vez que abres el estudio con Ollama ya en marcha (icono de la llama junto al reloj), el estudio guarda los ajustes de rendimiento de Ollama (flash attention, caché comprimida, una sola petición a la vez) en tu usuario de Windows. **Cierra Ollama desde ese icono y vuelve a abrir StudioIA.exe una vez** para que se apliquen.
- El estudio precarga el modelo al arrancar, así que el primer mensaje ya no espera a la carga. Los modelos siguen en memoria 30 minutos tras usarlos (`KEEP_ALIVE`).
- Las respuestas aparecen palabra a palabra según se generan. El botón **■ Parar** corta una respuesta larga.
- `THINK=true` en `studio.env` activa el razonamiento de Qwen3: algo mejor en problemas difíciles, pero bastante más lento.
- Para el dictado, `WHISPER_LANGUAGE=es` evita la detección de idioma; cámbialo si hablas en otra lengua.

## 4. Uso

### Conversar con el equipo
Escribe en la caja inferior y pulsa **Enviar** (o Intro; Mayús+Intro hace salto de línea). En el selector superior eliges quién responde:

- **🧭 Equipo (supervisor)**: decide si responde él o reparte el trabajo entre los especialistas. Es la opción recomendada.
- **🔎 Investigador**: busca en tus documentos y en la memoria, y cita las fuentes.
- **💻 Programador**: escribe y ejecuta código Python, analiza datos y genera archivos.
- **✍️ Escritor**: redacta, resume, traduce y guarda documentos.
- **🎨 Artista visual**: analiza imágenes y genera ilustraciones (requiere ComfyUI, ver apartado 7).

Despliega **«Proceso del equipo»** en cada respuesta para ver qué agente trabajó, qué herramientas usó y con qué resultados.

### Imágenes y documentos
- Pulsa 📎, arrastra archivos a la ventana o pega una captura con Ctrl+V.
- Las imágenes las analiza el modelo de visión (fotos, capturas, gráficos, texto escrito).
- PDF, Word, CSV, código o texto se guardan en el workspace y se indexan para que los agentes los busquen.

### Voz
- 🎙️ dicta: pulsa para empezar, pulsa otra vez para terminar. La transcripción la hace Whisper en tu PC (la primera vez descarga el modelo de voz, unos 500 MB con `small`).
- 🔊 en la barra superior lee en voz alta todas las respuestas; «Escuchar» lee una sola.
- Para una voz natural en español, sigue `voices\LEEME.txt` (voz Piper). Sin ella se usa la voz de Windows.

### Workspace (📁)
Es la carpeta `workspace` junto al programa, compartida entre tú y los agentes. Desde el panel puedes subir, crear, editar, descargar y borrar archivos. **↻ Indexar** vuelve a preparar todos los documentos para la búsqueda.

### Memoria (🧠)
El estudio recuerda hechos importantes (tu nombre, tus proyectos, tus preferencias) y los usa en futuras conversaciones. Puedes ver, buscar, añadir y borrar recuerdos en el panel. Desactiva 🧠 en la barra superior si no quieres que guarde recuerdos de una conversación.

### Agentes (🤖)
Cambia nombre, instrucciones, modelo y herramientas de cada agente, o crea agentes nuevos. Pulsa **Guardar equipo**. **Restaurar** vuelve al equipo de fábrica.

## 5. Usarlo desde el móvil (app Android)

1. Abre `studio.env` y pon `LAN=true`. Guarda y reinicia StudioIA.exe.
2. Windows preguntará si permites el acceso en redes privadas: pulsa **Permitir**.
3. La ventana mostrará la dirección (por ejemplo `http://192.168.1.20:8765`), el **token** y un código QR.
4. Sigue el *Manual de la app Android*.

El token protege el estudio para que nadie más de tu red pueda usarlo. Se guarda en `data\token.txt`; bórralo para generar uno nuevo.

## 6. Dónde están tus datos

| Carpeta | Contenido |
|---|---|
| `data\studio.db` | Conversaciones |
| `data\qdrant` | Memoria a largo plazo e índice de documentos |
| `data\agents.json` | Tu equipo de agentes |
| `data\modelos-voz` | Modelo de transcripción |
| `workspace` | Tus archivos y los que crean los agentes |

Para hacer una copia de seguridad, copia `data` y `workspace`. Los modelos de Ollama están en `%USERPROFILE%\.ollama`.

## 7. Generar imágenes (opcional)

1. Instala ComfyUI Desktop desde <https://www.comfy.org/download> y descarga un modelo (por ejemplo SDXL o FLUX schnell) desde su gestor.
2. Con ComfyUI abierto, añade a `studio.env` la línea `COMFYUI_URL=http://127.0.0.1:8000` (usa el puerto que muestre ComfyUI; en la versión portátil suele ser 8188).
3. Reinicia StudioIA.exe. La insignia «Generar imágenes» pasará a verde.

## 8. Problemas frecuentes

| Síntoma | Solución |
|---|---|
| «Ollama no está en marcha» | Abre Ollama desde el menú Inicio o reinstálalo. Comprueba <http://127.0.0.1:11434>. |
| «Faltan N modelos» | Pestaña ⚙️ Modelos → «Descargar los que faltan». |
| Respuestas muy lentas | Usa el perfil Ligero o cierra programas que consuman RAM/GPU. |
| El micrófono no funciona | Permite el micrófono al navegador (icono del candado en la barra de direcciones). |
| «Transcripción no disponible» | La primera transcripción necesita Internet para descargar Whisper. |
| El móvil no encuentra el PC | `LAN=true`, misma Wi-Fi, y permite StudioIA.exe en el Firewall de Windows (redes privadas). |
| Puerto 8765 ocupado | Cambia `PORT=` en `studio.env`. |

## 9. Desinstalar
Cierra el estudio y borra su carpeta. Para quitar los modelos, desinstala Ollama desde *Configuración → Aplicaciones* y borra `%USERPROFILE%\.ollama`.
