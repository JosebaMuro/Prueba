# Studio IA Local · Manual de la app Android

La app **Studio IA** tiene dos modos, que eliges al abrirla y puedes cambiar cuando quieras:

| | 📱 Autónomo | 🖥️ Conectado al PC |
|---|---|---|
| Dónde se ejecuta la IA | En el propio móvil | En tu PC, por la Wi-Fi de casa |
| Necesita PC | No | Sí, con Studio IA Local abierto |
| Necesita Internet | Solo para descargar los modelos una vez | No |
| Modelos | Pequeños (1,7B a 4B) | Los que aguante tu PC (8B, 14B…) |
| Velocidad y calidad | Buena en móviles recientes | La mejor |
| Extras | Voz nativa de Android | Ejecución de Python, PDF/Word, generación de imágenes |

En ambos modos tus datos se quedan en tus dispositivos.

## 1. Requisitos
- Android 8.0 o superior, de 64 bits (arm64).
- **Modo autónomo:** 6 GB de RAM como mínimo (8 GB o más recomendado), procesador de 2018 en adelante y entre 2 y 5 GB libres para los modelos.
- **Modo conectado:** el PC con Studio IA Local encendido, con `LAN=true` en `studio.env` (ver *Manual de Windows*, apartado 5), y móvil y PC en la misma Wi-Fi.

## 2. Instalar la app
1. Copia `StudioIA-Android.apk` al móvil (cable USB, Google Drive, correo…) o descárgalo directamente en él.
2. Ábrelo desde la app *Archivos* o *Descargas*.
3. Android pedirá permiso para **instalar aplicaciones desconocidas** desde esa app: actívalo y vuelve atrás.
4. Pulsa **Instalar**. Si Play Protect avisa de que no conoce la app, pulsa **Instalar de todas formas**. Aparece porque la app no se distribuye por Google Play.

## 3. Modo autónomo (solo el móvil)
1. Abre la app y pulsa **Usar solo este móvil (autónomo)**.
2. Se abre la pestaña **⚙️ Modelos** con los datos de tu teléfono y el perfil recomendado para su memoria:
   - **Gemma 3 4B** (3,4 GB): conversa y **ve imágenes**. Para móviles con 8 GB de RAM o más.
   - **Qwen3 4B** (2,5 GB): solo texto, el más fiable usando herramientas.
   - **Qwen3 1.7B** (1,1 GB): solo texto y rápido, para móviles con 6 GB.
3. Pulsa **Descargar los que faltan** (mejor con Wi-Fi) o toca otro perfil para cambiarlo. La descarga se puede reanudar si se corta.
4. Cuando el indicador diga **Listo · local**, ya puedes conversar, incluso en modo avión.

Qué incluye el modo autónomo:
- **Equipo de agentes**: supervisor, investigador, analista, escritor y observador, editables en 🤖 Agentes.
- **Memoria persistente**: recuerda hechos sobre ti entre conversaciones (🧠 Memoria).
- **Workspace**: archivos de texto que tú y los agentes creáis, buscables por significado. Están en `Android/data/com.studioia.local/files/workspace`.
- **Visión** con el perfil Gemma 3: adjunta o haz una foto con 📎.
- **Voz**: 🎙️ usa el dictado de Android y 🔊 su síntesis de voz. Para que funcionen sin conexión, descarga el español en *Ajustes → Sistema → Idiomas → Reconocimiento de voz sin conexión*.
- **Otros modelos**: pega la URL de cualquier archivo `.gguf` en «Descargar modelo».

Consejos:
- La primera respuesta tarda más porque el modelo se carga en memoria. Cierra otras apps para dejarle RAM.
- Desde la versión 1.2 el motor usa la mitad de memoria para el contexto y reaprovecha lo ya procesado de la conversación, así que a partir del segundo mensaje empieza a responder antes. Las conversaciones muy largas se recortan a los últimos mensajes; para un tema nuevo, empieza una conversación nueva.
- El botón **■ Parar** corta una respuesta que se alarga.
- Las respuestas largas calientan el teléfono y gastan batería; es normal.
- Si Android cierra la app en segundo plano, el motor se detiene y vuelve a arrancar al abrirla.
- Con modelos tan pequeños, pide las cosas de una en una y con instrucciones claras.

## 4. Conectar con el PC
1. En el PC, arranca StudioIA.exe con `LAN=true`. La ventana muestra:
   `Servidor: http://192.168.1.20:8765   Token: 7F3A9C21`
2. Abre **Studio IA** en el móvil.
3. Pulsa **Buscar el estudio en mi red** y elige tu PC de la lista, o escribe la dirección a mano (`192.168.1.20:8765`; el puerto se añade solo si lo omites).
4. Escribe el **token** y pulsa **Conectar**.

La app recuerda la conexión. Para cambiar de PC, pulsa el botón Atrás en la pantalla principal y elige **Cambiar de modo o servidor**.

## 5. Uso
La interfaz es la misma en los dos modos, adaptada a pantalla táctil:
- **☰** (arriba a la izquierda): conversaciones.
- **▤** (arriba a la derecha): workspace, memoria, agentes y modelos.
- **📎**: adjunta fotos de la galería, **haz una foto con la cámara** o adjunta documentos.
- **🎙️**: pulsa para grabar y otra vez para terminar. La grabación se transcribe en el PC. La primera vez Android te pedirá permiso para usar el micrófono; acéptalo y vuelve a pulsar.
- **🔊**: el móvil lee las respuestas en voz alta.
- Los archivos del workspace se descargan en la carpeta *Descargas* del móvil.

Ejemplos:
- Haz una foto a una factura y escribe: «Extrae los importes y guárdalos en facturas.csv».
- Dicta: «Resume las notas de la reunión que subí ayer y dime las tareas pendientes».
- Foto de una pizarra: «Pásalo a limpio en Markdown».

## 6. Botón Atrás
- Dentro de una página: vuelve atrás.
- En la pantalla principal: menú con **Cambiar de modo o servidor**, **Recargar** y **Salir**.

## 7. Problemas frecuentes

| Síntoma | Solución |
|---|---|
| Autónomo: «El motor no arrancó» | Falta memoria: cierra apps o elige un perfil más pequeño. El mensaje de error aparece en ⚙️ Modelos. |
| Autónomo: la descarga falla | Comprueba la conexión y vuelve a pulsar; continúa donde se quedó. |
| Autónomo: «esta APK no incluye el motor» | La APK se compiló sin llama.cpp. Usa el modo conectado o una APK con motor. |
| «No se pudo conectar» | ¿El PC está encendido y StudioIA.exe abierto? ¿Misma Wi-Fi? ¿`LAN=true`? |
| La búsqueda no encuentra el PC | Escribe la IP a mano. Algunas redes de invitados o routers con «aislamiento de clientes» impiden que los dispositivos se vean. |
| «Token de acceso incorrecto» | Copia el token que muestra la ventana del PC (también en `data\token.txt`). |
| El Firewall bloquea | En el PC: *Seguridad de Windows → Firewall → Permitir una aplicación* → marca StudioIA.exe y python.exe en «Privada». |
| El micrófono no graba | *Ajustes → Aplicaciones → Studio IA → Permisos → Micrófono → Permitir*. |
| La cámara no aparece al adjuntar | Concede el permiso de cámara a la app y vuelve a pulsar 📎. |

## 8. Privacidad
La app no tiene publicidad ni analítica. En modo autónomo solo se conecta a Internet para descargar los modelos que tú pidas (desde huggingface.co); en modo conectado solo habla con la dirección de tu PC. La comunicación va por tu red local sin cifrar (HTTP), protegida por el token. No actives `LAN=true` en redes públicas (cafeterías, hoteles).

## 9. Alternativa sin app
Cualquier navegador del móvil puede abrir el estudio: escanea el código QR que muestra la ventana del PC. Algunos navegadores no permiten el micrófono en direcciones `http://` de la red local; la app sí lo permite.
