package com.studioia.local;

import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.lang.reflect.Method;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Motores de inferencia que corren dentro de la app (además de llama.cpp, que va como proceso aparte):
 *
 * - ONNX Runtime GenAI (Microsoft): modelos ONNX GenAI en CPU.
 * - ExecuTorch (PyTorch): archivos .pte; CPU (XNNPACK) o GPU (Vulkan) según cómo se exportó el modelo.
 * - LiteRT-LM (Google): archivos .litertlm; CPU o GPU (y NPU si el móvil trae las bibliotecas del fabricante).
 * - Qualcomm Genie (GenieX, solo en la APK para Snapdragon): NPU Hexagon, GPU Adreno o CPU.
 *
 * Solo hay un modelo cargado a la vez; todas las llamadas se serializan con el monitor de la clase.
 */
final class Engines {
    interface Sink {
        /** Devuelve false para parar la generación. */
        boolean text(String piece);
    }

    private final Activity act;
    private final File cacheDir;
    private String engine = "", key = "";
    private Object a, b;           // objetos del motor (modelo, tokenizer…)
    private long handle;           // LiteRT-LM
    private volatile boolean stop;
    private volatile long currentConv;

    Engines(Activity act) {
        this.act = act;
        this.cacheDir = new File(act.getCacheDir(), "engines");
        cacheDir.mkdirs();
    }

    private File lib(String name) {
        return new File(act.getApplicationInfo().nativeLibraryDir, name);
    }

    /** Qué motores trae esta APK y qué hardware tiene el móvil. */
    JSONObject info() {
        JSONObject o = new JSONObject();
        try {
            JSONObject e = new JSONObject();
            e.put("llamacpp", lib("libllama-server.so").exists());
            e.put("onnx", lib("libonnxruntime-genai-jni.so").exists());
            e.put("executorch", lib("libexecutorch.so").exists());
            e.put("litert", lib("liblitertlm_jni.so").exists());
            e.put("genie", lib("libgeniex.so").exists());
            o.put("engines", e);
            String soc = "", socMaker = "";
            if (Build.VERSION.SDK_INT >= 31) {
                soc = Build.SOC_MODEL;
                socMaker = Build.SOC_MANUFACTURER;
            }
            o.put("soc", soc);
            o.put("socMaker", socMaker);
            o.put("hardware", Build.HARDWARE);
            o.put("model", Build.MANUFACTURER + " " + Build.MODEL);
            String all = (socMaker + " " + soc + " " + Build.HARDWARE + " " + Build.BOARD).toLowerCase();
            o.put("qualcomm", all.contains("qti") || all.contains("qualcomm") || all.contains("qcom") || all.matches(".*\\bsm\\d{4}.*"));
            o.put("mediatek", all.contains("mediatek") || all.contains("mt6") || all.contains(" mt"));
            PackageManager pm = act.getPackageManager();
            o.put("vulkan", pm.hasSystemFeature(PackageManager.FEATURE_VULKAN_HARDWARE_VERSION, 0x401000));
            o.put("loaded", engine + "|" + key);
        } catch (Exception ignored) {
        }
        return o;
    }

    // ------------------------------------------------------------------ carga

    synchronized void load(String eng, String path, String tokenizer, String placement, int ctx, float temperature) throws Exception {
        String k = path + "|" + placement + "|" + ctx;
        if (eng.equals(engine) && k.equals(key)) return;
        unload();
        if (eng.equals("onnx")) {
            ai.onnxruntime.genai.Config cfg = new ai.onnxruntime.genai.Config(path);
            cfg.clearProviders();  // CPU (la GPU y la NPU no tienen proveedor en la versión de Android)
            ai.onnxruntime.genai.Model m = new ai.onnxruntime.genai.Model(cfg);
            a = m;
            b = new ai.onnxruntime.genai.Tokenizer(m);
        } else if (eng.equals("executorch")) {
            if (!com.facebook.soloader.nativeloader.NativeLoader.isInitialized()) {
                com.facebook.soloader.nativeloader.NativeLoader.init(new com.facebook.soloader.nativeloader.SystemDelegate());
            }
            System.loadLibrary("executorch");
            if (tokenizer == null || tokenizer.isEmpty()) throw new Exception("Falta el tokenizer junto al archivo .pte");
            org.pytorch.executorch.extension.llm.LlmModule m =
                    new org.pytorch.executorch.extension.llm.LlmModule(path, tokenizer, temperature);
            m.load();
            a = m;
        } else if (eng.equals("litert")) {
            LiteRt.load();
            String backend = placement.equals("gpu") ? "gpu" : placement.equals("npu") ? "npu" : "cpu";
            handle = LiteRt.engineCreate(path, backend, ctx, cacheDir.getAbsolutePath(),
                    act.getApplicationInfo().nativeLibraryDir);
            if (handle == 0) throw new Exception("LiteRT-LM no pudo cargar el modelo en " + backend.toUpperCase() + ": " + LiteRt.error());
        } else if (eng.equals("genie")) {
            Class<?> c = Class.forName("com.studioia.local.GenieXEngine");
            Object g = c.getConstructor(Activity.class).newInstance(act);
            c.getMethod("load", String.class, String.class, String.class, int.class).invoke(g, path, tokenizer, placement, ctx);
            a = g;
        } else {
            throw new Exception("Motor desconocido: " + eng);
        }
        engine = eng;
        key = k;
    }

    synchronized void unload() {
        try {
            if (engine.equals("onnx")) {
                ((ai.onnxruntime.genai.Tokenizer) b).close();
                ((ai.onnxruntime.genai.Model) a).close();
            } else if (engine.equals("executorch")) {
                ((org.pytorch.executorch.extension.llm.LlmModule) a).close();
            } else if (engine.equals("litert") && handle != 0) {
                LiteRt.engineDelete(handle);
            } else if (engine.equals("genie") && a != null) {
                a.getClass().getMethod("close").invoke(a);
            }
        } catch (Throwable ignored) {
        }
        a = b = null;
        handle = 0;
        engine = key = "";
        System.gc();
    }

    void cancel() {
        stop = true;
        try {
            if (engine.equals("executorch") && a != null) ((org.pytorch.executorch.extension.llm.LlmModule) a).stop();
            if (engine.equals("litert") && currentConv != 0) LiteRt.cancel(currentConv);
            if (engine.equals("genie") && a != null) a.getClass().getMethod("stop").invoke(a);
        } catch (Throwable ignored) {
        }
    }

    // ------------------------------------------------------------------ generación

    /**
     * req: {"prompt": texto ya formateado (ONNX/ExecuTorch), "system", "history": [...], "message": "..."
     * (LiteRT-LM y Genie aplican su propia plantilla), "maxTokens", "temperature", "ctx"}
     */
    synchronized void run(JSONObject req, final Sink sink) throws Exception {
        stop = false;
        int maxTokens = req.optInt("maxTokens", 1024);
        float temp = (float) req.optDouble("temperature", 0.6);
        if (engine.equals("onnx")) {
            ai.onnxruntime.genai.Model m = (ai.onnxruntime.genai.Model) a;
            ai.onnxruntime.genai.Tokenizer t = (ai.onnxruntime.genai.Tokenizer) b;
            ai.onnxruntime.genai.Sequences seq = t.encode(req.getString("prompt"));
            int len = seq.getSequence(0).length;
            ai.onnxruntime.genai.GeneratorParams p = new ai.onnxruntime.genai.GeneratorParams(m);
            p.setSearchOption("max_length", Math.max(len + 16, Math.min(req.optInt("ctx", 4096), len + maxTokens)));
            if (temp > 0) {
                p.setSearchOption("do_sample", true);
                p.setSearchOption("temperature", temp);
                p.setSearchOption("top_p", 0.95);
                p.setSearchOption("top_k", 40);
            }
            ai.onnxruntime.genai.Generator g = new ai.onnxruntime.genai.Generator(m, p);
            ai.onnxruntime.genai.TokenizerStream ts = t.createStream();
            try {
                g.appendTokenSequences(seq);
                while (!g.isDone() && !stop) {
                    g.generateNextToken();
                    if (!sink.text(ts.decode(g.getLastTokenInSequence(0)))) break;
                }
            } finally {
                ts.close();
                g.close();
                p.close();
                seq.close();
            }
        } else if (engine.equals("executorch")) {
            final org.pytorch.executorch.extension.llm.LlmModule m = (org.pytorch.executorch.extension.llm.LlmModule) a;
            m.resetContext();
            m.generate(req.getString("prompt"), Math.min(req.optInt("ctx", 4096), 8192),
                    new org.pytorch.executorch.extension.llm.LlmCallback() {
                        public void onResult(String s) {
                            if (!stop && !sink.text(s)) {
                                stop = true;
                                m.stop();
                            }
                        }
                    }, false);
        } else if (engine.equals("litert")) {
            long conv = LiteRt.conversationCreate(handle, req.optString("system", ""), req.optJSONArray("history") == null ? "[]" : req.getJSONArray("history").toString());
            if (conv == 0) throw new Exception("LiteRT-LM: " + LiteRt.error());
            currentConv = conv;
            final CountDownLatch done = new CountDownLatch(1);
            final String[] err = {null};
            try {
                JSONObject msg = new JSONObject().put("role", "user").put("content", req.optString("message", ""));
                int rc = LiteRt.send(conv, msg.toString(), new LiteRt.Listener() {
                    public void onChunk(byte[] text, boolean last, byte[] error) {
                        try {
                            if (error != null) {
                                String e = new String(error, "UTF-8");
                                if (!e.contains("CANCELLED") && !e.contains("Max number of tokens")) err[0] = e;
                            } else if (text != null && text.length > 0) {
                                if (!stop && !sink.text(liteRtText(new String(text, "UTF-8")))) cancel();
                            }
                        } catch (Exception ignored) {
                        }
                        if (last) done.countDown();
                    }
                });
                if (rc != 0) throw new Exception("LiteRT-LM no pudo generar: " + LiteRt.error());
                if (!done.await(30, TimeUnit.MINUTES)) cancel();
                if (err[0] != null) throw new Exception("LiteRT-LM: " + err[0]);
            } finally {
                currentConv = 0;
                LiteRt.conversationDelete(conv);
            }
        } else if (engine.equals("genie")) {
            Method r = a.getClass().getMethod("run", JSONObject.class, Sink.class);
            r.invoke(a, req, sink);
        } else {
            throw new Exception("No hay ningún modelo cargado");
        }
    }

    /** Los trozos de LiteRT-LM son mensajes JSON: {"role":"assistant","content":[{"type":"text","text":"…"}]}. */
    static String liteRtText(String chunk) {
        try {
            JSONObject o = new JSONObject(chunk);
            Object c = o.opt("content");
            if (c instanceof String) return (String) c;
            StringBuilder sb = new StringBuilder();
            JSONArray arr = (JSONArray) c;
            for (int i = 0; arr != null && i < arr.length(); i++) sb.append(arr.getJSONObject(i).optString("text", ""));
            return sb.toString();
        } catch (Exception e) {
            return chunk;
        }
    }
}
