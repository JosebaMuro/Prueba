package com.studioia.local;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLDecoder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Modo autónomo: todo el estudio dentro del móvil.
 *
 * - Motor: llama-server (llama.cpp) empaquetado como lib/arm64-v8a/libllama-server.so y lanzado
 *   como proceso local en 127.0.0.1 (chat/visión en el puerto 8431, embeddings en el 8432).
 * - La interfaz es la misma web del estudio, servida desde los assets en http://studio.local/.
 *   Las rutas /api las resuelve local.js (agentes, memoria, workspace) usando este puente.
 * - Voz: reconocimiento y síntesis nativos de Android (funcionan sin conexión si el idioma está instalado).
 */
public class LocalStudio {
    static final String ORIGIN = "http://studio.local/";
    static final int PORT_CHAT = 8431;
    static final int PORT_EMBED = 8432;

    private final Activity act;
    private final WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final File root, models, workspace, kv;
    private Process chatProc, embedProc;
    private String chatModel = "", engineError = "";
    private final Map<String, Boolean> cancelled = new HashMap<String, Boolean>();
    private SpeechRecognizer recognizer;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private final Engines engines;

    LocalStudio(Activity act, WebView web) {
        this.act = act;
        this.web = web;
        File ext = act.getExternalFilesDir(null);
        root = ext != null ? ext : act.getFilesDir();
        models = new File(root, "models");
        workspace = new File(root, "workspace");
        kv = new File(act.getFilesDir(), "data");
        models.mkdirs();
        workspace.mkdirs();
        engines = new Engines(act);
        kv.mkdirs();
    }

    // ------------------------------------------------------------------ servir la interfaz

    /** Atiende http://studio.local/... desde los assets y los archivos del workspace. */
    WebResourceResponse intercept(String url) {
        if (!url.startsWith(ORIGIN)) return null;
        String path = url.substring(ORIGIN.length());
        String query = "";
        int q = path.indexOf('?');
        if (q >= 0) {
            query = path.substring(q + 1);
            path = path.substring(0, q);
        }
        try {
            if (path.equals("api/workspace/raw")) {
                String rel = "";
                for (String kvp : query.split("&")) {
                    if (kvp.startsWith("path=")) rel = URLDecoder.decode(kvp.substring(5), "UTF-8");
                }
                File f = resolve(rel);
                return new WebResourceResponse(mime(rel), null, new FileInputStream(f));
            }
            if (path.isEmpty()) path = "index.html";
            InputStream in = act.getAssets().open("www/" + path);
            if (path.equals("index.html")) {
                String html = new String(readAll(in), "UTF-8").replace("<script src=\"app.js\">", "<script src=\"local.js\"></script><script src=\"app.js\">");
                return new WebResourceResponse("text/html", "UTF-8", new ByteArrayInputStream(html.getBytes("UTF-8")));
            }
            return new WebResourceResponse(mime(path), path.matches(".*\\.(html|js|css|svg|webmanifest)$") ? "UTF-8" : null, in);
        } catch (Exception e) {
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found", null,
                    new ByteArrayInputStream(String.valueOf(e.getMessage()).getBytes()));
        }
    }

    private static String mime(String name) {
        String ext = name.contains(".") ? name.substring(name.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT) : "";
        if (ext.equals("js")) return "application/javascript";
        if (ext.equals("svg")) return "image/svg+xml";
        if (ext.equals("webmanifest")) return "application/manifest+json";
        String m = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
        return m != null ? m : "application/octet-stream";
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        in.close();
        return bos.toByteArray();
    }

    private File resolve(String rel) throws Exception {
        File f = new File(workspace, rel).getCanonicalFile();
        String base = workspace.getCanonicalPath();
        if (!f.getPath().equals(base) && !f.getPath().startsWith(base + File.separator)) {
            throw new SecurityException("Ruta fuera del workspace: " + rel);
        }
        return f;
    }

    /** Archivo dentro de la carpeta de modelos (admite subcarpetas: org--modelo/archivo). */
    private File modelPath(String rel) throws Exception {
        File f = new File(models, rel).getCanonicalFile();
        String base = models.getCanonicalPath();
        if (!f.getPath().startsWith(base + File.separator)) throw new SecurityException("Ruta fuera de models: " + rel);
        return f;
    }

    private static long sizeOf(File f) {
        if (f.isFile()) return f.length();
        long t = 0;
        File[] fs = f.listFiles();
        if (fs != null) for (File x : fs) t += sizeOf(x);
        return t;
    }

    private static void deleteTree(File f) {
        File[] fs = f.listFiles();
        if (fs != null) for (File x : fs) deleteTree(x);
        f.delete();
    }

    /** Envía un evento a local.js: window.__native(id, tipo, datos). */
    private void emit(final String id, final String type, final String data) {
        ui.post(new Runnable() {
            public void run() {
                web.evaluateJavascript("window.__native && window.__native(" + JSONObject.quote(id) + ","
                        + JSONObject.quote(type) + "," + JSONObject.quote(data == null ? "" : data) + ")", null);
            }
        });
    }

    // ------------------------------------------------------------------ motor llama.cpp

    private File engineBinary() {
        return new File(act.getApplicationInfo().nativeLibraryDir, "libllama-server.so");
    }

    private Process launch(List<String> args, String logName) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(args);
        pb.redirectErrorStream(true);
        pb.redirectOutput(new File(act.getCacheDir(), logName));
        pb.directory(root);
        return pb.start();
    }

    private static boolean alive(Process p) {
        if (p == null) return false;
        try {
            p.exitValue();
            return false;
        } catch (IllegalThreadStateException e) {
            return true;
        }
    }

    private boolean healthy(int port) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/health").openConnection();
            c.setConnectTimeout(500);
            c.setReadTimeout(1500);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private boolean waitHealthy(Process p, int port, int seconds) throws InterruptedException {
        for (int i = 0; i < seconds * 2; i++) {
            if (!alive(p)) return false;
            if (healthy(port)) return true;
            Thread.sleep(500);
        }
        return false;
    }

    private String tail(String logName) {
        try {
            String s = new String(readAll(new FileInputStream(new File(act.getCacheDir(), logName))), "UTF-8");
            return s.length() > 1500 ? s.substring(s.length() - 1500) : s;
        } catch (Exception e) {
            return "";
        }
    }

    synchronized void stopEngine() {
        if (chatProc != null) chatProc.destroy();
        if (embedProc != null) embedProc.destroy();
        chatProc = null;
        embedProc = null;
        chatModel = "";
    }

    void destroy() {
        stopEngine();
        engines.unload();
        if (recognizer != null) recognizer.destroy();
        if (tts != null) tts.shutdown();
    }

    // ------------------------------------------------------------------ API para local.js

    public class Bridge {
        @JavascriptInterface
        public boolean isLocal() {
            return true;
        }

        // ---- almacenamiento clave/valor (conversaciones, memoria, agentes)
        @JavascriptInterface
        public String kvGet(String name) {
            try {
                return new String(readAll(new FileInputStream(new File(kv, name + ".json"))), "UTF-8");
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public void kvSet(String name, String value) {
            try {
                File tmp = new File(kv, name + ".tmp");
                FileOutputStream out = new FileOutputStream(tmp);
                out.write(value.getBytes("UTF-8"));
                out.close();
                tmp.renameTo(new File(kv, name + ".json"));
            } catch (Exception ignored) {
            }
        }

        // ---- workspace
        @JavascriptInterface
        public String wsList() {
            JSONArray out = new JSONArray();
            try {
                walk(workspace, "", out);
            } catch (Exception ignored) {
            }
            return out.toString();
        }

        private void walk(File dir, String prefix, JSONArray out) throws Exception {
            File[] files = dir.listFiles();
            if (files == null) return;
            java.util.Arrays.sort(files);
            for (File f : files) {
                if (f.getName().startsWith(".")) continue;
                JSONObject o = new JSONObject();
                o.put("path", prefix + f.getName());
                o.put("dir", f.isDirectory());
                o.put("size", f.isFile() ? f.length() : 0);
                out.put(o);
                if (f.isDirectory()) walk(f, prefix + f.getName() + "/", out);
            }
        }

        @JavascriptInterface
        public String wsRead(String path) throws Exception {
            byte[] b = readAll(new FileInputStream(resolve(path)));
            return new String(b, 0, Math.min(b.length, 200000), "UTF-8");
        }

        @JavascriptInterface
        public String wsReadB64(String path) throws Exception {
            return Base64.encodeToString(readAll(new FileInputStream(resolve(path))), Base64.NO_WRAP);
        }

        @JavascriptInterface
        public void wsWrite(String path, String text) throws Exception {
            write(path, text.getBytes("UTF-8"));
        }

        @JavascriptInterface
        public void wsWriteB64(String path, String b64) throws Exception {
            write(path, Base64.decode(b64, Base64.DEFAULT));
        }

        private void write(String path, byte[] data) throws Exception {
            File f = resolve(path);
            f.getParentFile().mkdirs();
            FileOutputStream out = new FileOutputStream(f);
            out.write(data);
            out.close();
        }

        @JavascriptInterface
        public void wsDelete(String path) throws Exception {
            File f = resolve(path);
            if (f.equals(workspace.getCanonicalFile())) return;
            rm(f);
        }

        private void rm(File f) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) rm(k);
            f.delete();
        }

        // ---- modelos
        @JavascriptInterface
        public String modelFiles() {
            JSONObject o = new JSONObject();
            try {
                File[] fs = models.listFiles();
                if (fs != null) for (File f : fs) if (f.getName().endsWith(".gguf")) o.put(f.getName(), f.length());
            } catch (Exception ignored) {
            }
            return o.toString();
        }

        @JavascriptInterface
        public void deleteModel(String file) {
            try {
                deleteTree(modelPath(file));
            } catch (Exception ignored) {
            }
        }

        /** Modelos de la carpeta models: [{name, dir, size, files: [nombres de primer nivel]}]. */
        @JavascriptInterface
        public String modelList() {
            JSONArray out = new JSONArray();
            try {
                File[] fs = models.listFiles();
                if (fs != null) for (File f : fs) {
                    if (f.getName().endsWith(".part") || f.getName().startsWith(".")) continue;
                    JSONObject o = new JSONObject();
                    o.put("name", f.getName());
                    o.put("dir", f.isDirectory());
                    o.put("size", sizeOf(f));
                    JSONArray files = new JSONArray();
                    File[] inner = f.isDirectory() ? f.listFiles() : null;
                    if (inner != null) for (File x : inner) if (!x.getName().endsWith(".part")) files.put(x.getName());
                    o.put("files", files);
                    out.put(o);
                }
            } catch (Exception ignored) {
            }
            return out.toString();
        }

        /** Lee un archivo de texto de la carpeta de modelos (plantillas, configuración). Máx. 4 MB. */
        @JavascriptInterface
        public String modelRead(String rel) {
            try {
                File f = modelPath(rel);
                if (f.length() > 4 << 20) return "";
                return new String(readAll(new FileInputStream(f)), "UTF-8");
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public String modelAbsPath(String rel) {
            try {
                return modelPath(rel).getAbsolutePath();
            } catch (Exception e) {
                return "";
            }
        }

        // ---- motores dentro de la app (ONNX Runtime GenAI, ExecuTorch, LiteRT-LM, Genie)
        @JavascriptInterface
        public String engineInfo() {
            return engines.info().toString();
        }

        /** {"engine","model","tokenizer","placement","ctx","temperature"} → done / error */
        @JavascriptInterface
        public void genLoad(final String id, final String json) {
            new Thread(new Runnable() {
                public void run() {
                    try {
                        JSONObject o = new JSONObject(json);
                        String tok = o.optString("tokenizer", "");
                        engines.load(o.getString("engine"), modelPath(o.getString("model")).getAbsolutePath(),
                                tok.isEmpty() ? "" : modelPath(tok).getAbsolutePath(), o.optString("placement", "cpu"),
                                o.optInt("ctx", 4096), (float) o.optDouble("temperature", 0.6));
                        emit(id, "done", "");
                    } catch (Throwable e) {
                        emit(id, "error", e.getClass().getSimpleName() + ": " + e.getMessage());
                    }
                }
            }).start();
        }

        /** Genera con el modelo cargado. Eventos: chunk (texto), done, error. */
        @JavascriptInterface
        public void genRun(final String id, final String json) {
            cancelled.put(id, false);
            new Thread(new Runnable() {
                public void run() {
                    try {
                        engines.run(new JSONObject(json), new Engines.Sink() {
                            public boolean text(String piece) {
                                if (Boolean.TRUE.equals(cancelled.get(id))) return false;
                                if (piece != null && !piece.isEmpty()) emit(id, "chunk", piece);
                                return true;
                            }
                        });
                        emit(id, "done", "");
                    } catch (Throwable e) {
                        Throwable c = e.getCause() != null ? e.getCause() : e;
                        emit(id, "error", c.getClass().getSimpleName() + ": " + c.getMessage());
                    }
                }
            }).start();
        }

        @JavascriptInterface
        public void genStop() {
            engines.cancel();
        }

        @JavascriptInterface
        public void genUnload() {
            new Thread(new Runnable() {
                public void run() {
                    engines.unload();
                }
            }).start();
        }

        @JavascriptInterface
        public String deviceInfo() {
            JSONObject o = new JSONObject();
            try {
                android.app.ActivityManager.MemoryInfo mi = new android.app.ActivityManager.MemoryInfo();
                ((android.app.ActivityManager) act.getSystemService(Activity.ACTIVITY_SERVICE)).getMemoryInfo(mi);
                o.put("ramGb", Math.round(mi.totalMem / 1e9 * 10) / 10.0);
                o.put("cores", Runtime.getRuntime().availableProcessors());
                o.put("freeGb", Math.round(models.getUsableSpace() / 1e9 * 10) / 10.0);
                o.put("engine", engineBinary().exists());
                o.put("abi", android.os.Build.SUPPORTED_ABIS[0]);
            } catch (Exception ignored) {
            }
            return o.toString();
        }

        /** Descarga con reanudación. Eventos: progress "{completed,total}", done, error. */
        @JavascriptInterface
        public void download(final String id, final String url, final String file) {
            cancelled.put(id, false);
            new Thread(new Runnable() {
                public void run() {
                    HttpURLConnection c = null;
                    File part = null, dest = null;
                    try {
                        dest = modelPath(file);
                        dest.getParentFile().mkdirs();
                        part = new File(dest.getPath() + ".part");
                        long have = part.exists() ? part.length() : 0;
                        c = (HttpURLConnection) new URL(url).openConnection();
                        c.setInstanceFollowRedirects(true);
                        c.setConnectTimeout(15000);
                        c.setReadTimeout(30000);
                        if (have > 0) c.setRequestProperty("Range", "bytes=" + have + "-");
                        int code = c.getResponseCode();
                        if (code == 200) have = 0;
                        else if (code != 206) throw new Exception("HTTP " + code);
                        long total = have + c.getContentLengthLong();
                        RandomAccessFile out = new RandomAccessFile(part, "rw");
                        out.seek(have);
                        InputStream in = c.getInputStream();
                        byte[] buf = new byte[1 << 16];
                        int n;
                        long last = 0;
                        while ((n = in.read(buf)) > 0) {
                            if (Boolean.TRUE.equals(cancelled.get(id))) {
                                out.close();
                                emit(id, "error", "Descarga cancelada");
                                return;
                            }
                            out.write(buf, 0, n);
                            have += n;
                            long now = System.currentTimeMillis();
                            if (now - last > 400) {
                                last = now;
                                emit(id, "progress", "{\"completed\":" + have + ",\"total\":" + total + "}");
                            }
                        }
                        out.close();
                        part.renameTo(dest);
                        emit(id, "done", "");
                    } catch (Exception e) {
                        emit(id, "error", String.valueOf(e.getMessage()));
                    } finally {
                        if (c != null) c.disconnect();
                    }
                }
            }).start();
        }

        @JavascriptInterface
        public void cancel(String id) {
            cancelled.put(id, true);
        }

        // ---- motor
        @JavascriptInterface
        public String engineStatus() {
            JSONObject o = new JSONObject();
            try {
                o.put("chat", alive(chatProc) && healthy(PORT_CHAT));
                o.put("embed", alive(embedProc) && healthy(PORT_EMBED));
                o.put("model", chatModel);
                o.put("error", engineError);
            } catch (Exception ignored) {
            }
            return o.toString();
        }

        private List<String> chatArgs(File bin, String model, String mmproj, int ctx, int threads, boolean tuned) {
            List<String> a = new ArrayList<String>();
            a.add(bin.getAbsolutePath());
            a.add("-m"); a.add(new File(models, model).getAbsolutePath());
            if (!mmproj.isEmpty()) { a.add("--mmproj"); a.add(new File(models, mmproj).getAbsolutePath()); }
            a.add("--host"); a.add("127.0.0.1");
            a.add("--port"); a.add(String.valueOf(PORT_CHAT));
            a.add("-c"); a.add(String.valueOf(ctx));
            a.add("-t"); a.add(String.valueOf(threads));
            a.add("--jinja");
            a.add("--no-webui");
            if (tuned) {
                a.add("--parallel"); a.add("1");
                a.add("--flash-attn"); a.add("on");
                a.add("--cache-type-k"); a.add("q8_0");
                a.add("--cache-type-v"); a.add("q8_0");
            }
            return a;
        }

        /** Arranca (o reinicia) el motor. Eventos: done / error. mmproj y embed pueden ir vacíos. */
        @JavascriptInterface
        public void startEngine(final String id, final String model, final String mmproj, final String embed, final int ctx) {
            new Thread(new Runnable() {
                public void run() {
                    synchronized (LocalStudio.this) {
                        try {
                            engineError = "";
                            File bin = engineBinary();
                            if (!bin.exists()) throw new Exception("Esta APK no incluye el motor llama.cpp para " + android.os.Build.SUPPORTED_ABIS[0]);
                            int threads = Math.max(2, Math.min(6, Runtime.getRuntime().availableProcessors() / 2));
                            if (!(alive(chatProc) && chatModel.equals(model + "|" + mmproj) && healthy(PORT_CHAT))) {
                                if (chatProc != null) chatProc.destroy();
                                // 1.er intento con ajustes de rendimiento: una sola ranura (todo el contexto para la
                                // conversación), flash attention y caché KV en 8 bits (la mitad de RAM).
                                // Si este motor o el móvil no los admiten, se reintenta con la configuración básica.
                                chatProc = launch(chatArgs(bin, model, mmproj, ctx, threads, true), "engine-chat.log");
                                if (!waitHealthy(chatProc, PORT_CHAT, 180)) {
                                    String first = tail("engine-chat.log");
                                    if (chatProc != null) chatProc.destroy();
                                    chatProc = launch(chatArgs(bin, model, mmproj, ctx, threads, false), "engine-chat.log");
                                    if (!waitHealthy(chatProc, PORT_CHAT, 180)) throw new Exception("El motor no arrancó:\n" + tail("engine-chat.log") + "\n\n(1.er intento)\n" + first);
                                }
                                chatModel = model + "|" + mmproj;
                            }
                            if (!embed.isEmpty() && !(alive(embedProc) && healthy(PORT_EMBED))) {
                                List<String> a = new ArrayList<String>();
                                a.add(bin.getAbsolutePath());
                                a.add("-m"); a.add(new File(models, embed).getAbsolutePath());
                                a.add("--embedding");
                                a.add("--pooling"); a.add("mean");
                                a.add("--host"); a.add("127.0.0.1");
                                a.add("--port"); a.add(String.valueOf(PORT_EMBED));
                                a.add("-c"); a.add("2048");
                                // lotes del tamaño del contexto: si no, los textos de más de 512 tokens fallan
                                a.add("--batch-size"); a.add("2048");
                                a.add("--ubatch-size"); a.add("2048");
                                a.add("--parallel"); a.add("1");
                                a.add("-t"); a.add("2");
                                a.add("--no-webui");
                                embedProc = launch(a, "engine-embed.log");
                                if (!waitHealthy(embedProc, PORT_EMBED, 60)) throw new Exception("El motor de memoria no arrancó:\n" + tail("engine-embed.log"));
                            }
                            emit(id, "done", "");
                        } catch (Exception e) {
                            engineError = String.valueOf(e.getMessage());
                            emit(id, "error", engineError);
                        }
                    }
                }
            }).start();
        }

        @JavascriptInterface
        public void stopEngine() {
            LocalStudio.this.stopEngine();
        }

        /** POST JSON al motor local. Con stream=true emite cada línea SSE como "chunk". Eventos: chunk, done(cuerpo), error. */
        @JavascriptInterface
        public void http(final String id, final int port, final String path, final String body, final boolean stream) {
            cancelled.put(id, false);
            new Thread(new Runnable() {
                public void run() {
                    HttpURLConnection c = null;
                    try {
                        c = (HttpURLConnection) new URL("http://127.0.0.1:" + port + path).openConnection();
                        c.setConnectTimeout(3000);
                        c.setReadTimeout(600000);
                        c.setDoOutput(true);
                        c.setRequestProperty("Content-Type", "application/json");
                        OutputStream os = c.getOutputStream();
                        os.write(body.getBytes("UTF-8"));
                        os.close();
                        int code = c.getResponseCode();
                        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
                        if (code >= 400) throw new Exception("HTTP " + code + ": " + new String(readAll(in), "UTF-8"));
                        if (!stream) {
                            emit(id, "done", new String(readAll(in), "UTF-8"));
                            return;
                        }
                        BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"));
                        String line;
                        while ((line = r.readLine()) != null) {
                            if (Boolean.TRUE.equals(cancelled.get(id))) break;
                            if (line.startsWith("data: ")) emit(id, "chunk", line.substring(6));
                        }
                        emit(id, "done", "");
                    } catch (Exception e) {
                        emit(id, "error", String.valueOf(e.getMessage()));
                    } finally {
                        if (c != null) c.disconnect();
                    }
                }
            }).start();
        }

        // ---- voz nativa
        @JavascriptInterface
        public void speak(final String text) {
            ui.post(new Runnable() {
                public void run() {
                    if (tts == null) {
                        tts = new TextToSpeech(act, new TextToSpeech.OnInitListener() {
                            public void onInit(int status) {
                                ttsReady = status == TextToSpeech.SUCCESS;
                                if (ttsReady) {
                                    tts.setLanguage(Locale.getDefault());
                                    tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "studio");
                                }
                            }
                        });
                    } else if (ttsReady) {
                        if (tts.isSpeaking()) tts.stop();
                        else tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "studio");
                    }
                }
            });
        }

        /** Dictado nativo. Eventos sobre el id "speech": partial(texto), done(texto), error. */
        @JavascriptInterface
        public void listen() {
            ui.post(new Runnable() {
                public void run() {
                    if (act.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                        act.requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO}, 20);
                        emit("speech", "error", "Concede el permiso de micrófono y vuelve a pulsar");
                        return;
                    }
                    if (!SpeechRecognizer.isRecognitionAvailable(act)) {
                        emit("speech", "error", "Este móvil no tiene reconocimiento de voz instalado");
                        return;
                    }
                    if (recognizer != null) recognizer.destroy();
                    recognizer = SpeechRecognizer.createSpeechRecognizer(act);
                    recognizer.setRecognitionListener(new RecognitionListener() {
                        public void onResults(Bundle b) {
                            ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                            emit("speech", "done", r != null && !r.isEmpty() ? r.get(0) : "");
                        }

                        public void onPartialResults(Bundle b) {
                            ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                            if (r != null && !r.isEmpty()) emit("speech", "partial", r.get(0));
                        }

                        public void onError(int error) {
                            emit("speech", "error", error == SpeechRecognizer.ERROR_NO_MATCH ? "No te he entendido" : "Error de voz " + error);
                        }

                        public void onReadyForSpeech(Bundle b) {
                        }

                        public void onBeginningOfSpeech() {
                        }

                        public void onRmsChanged(float v) {
                        }

                        public void onBufferReceived(byte[] b) {
                        }

                        public void onEndOfSpeech() {
                        }

                        public void onEvent(int i, Bundle b) {
                        }
                    });
                    Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                    i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
                    i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
                    i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
                    recognizer.startListening(i);
                }
            });
        }

        @JavascriptInterface
        public void stopListening() {
            ui.post(new Runnable() {
                public void run() {
                    if (recognizer != null) recognizer.stopListening();
                }
            });
        }

        @JavascriptInterface
        public void changeServer() {
            ui.post(new Runnable() {
                public void run() {
                    ((MainActivity) act).openSetup();
                }
            });
        }
    }
}
