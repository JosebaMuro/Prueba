package com.studioia.local;

import android.app.Activity;

import com.geniex.sdk.GenieXSdk;
import com.geniex.sdk.bean.ChatMessage;
import com.geniex.sdk.bean.GenerationConfig;
import com.geniex.sdk.bean.LLMTokenCallback;
import com.geniex.sdk.bean.LlmCreateInput;
import com.geniex.sdk.bean.LlmGenerateResult;
import com.geniex.sdk.bean.ModelConfig;
import com.geniex.sdk.bean.SamplerConfig;
import com.geniex.sdk.jni.Llm;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Qualcomm Genie en Android a través de GenieX (SDK de Qualcomm publicado en Maven Central).
 * Solo va en la APK para Snapdragon. Dos runtimes:
 *  - "qairt": modelos de Qualcomm AI Hub compilados para la NPU Hexagon (carpeta con genie_config.json o .bin).
 *  - "llama_cpp": archivos .gguf, en CPU, GPU Adreno o NPU Hexagon (backend de ggml de Qualcomm).
 * La unidad de cómputo sale de la ubicación elegida: npu, gpu o cpu.
 */
public final class GenieXEngine {
    private static boolean sdkReady;
    private final Activity act;
    private final Llm llm = new Llm();
    private long handle;
    private volatile boolean stop;

    public GenieXEngine(Activity act) {
        this.act = act;
    }

    private void initSdk() throws Exception {
        if (sdkReady) return;
        final CountDownLatch latch = new CountDownLatch(1);
        final String[] err = {null};
        GenieXSdk.Companion.getInstance().init(act.getApplicationContext(), new GenieXSdk.InitCallback() {
            public void onSuccess() {
                latch.countDown();
            }

            public void onFailure(String reason) {
                err[0] = reason;
                latch.countDown();
            }
        });
        if (!latch.await(60, TimeUnit.SECONDS)) throw new Exception("GenieX no respondió al iniciar");
        if (err[0] != null) throw new Exception("GenieX: " + err[0]);
        sdkReady = true;
    }

    public void load(String path, String tokenizer, String placement, int ctx) throws Exception {
        initSdk();
        boolean gguf = path.toLowerCase().endsWith(".gguf");
        String runtime = gguf ? GenieXSdk.PLUGIN_ID_LLAMA_CPP : GenieXSdk.PLUGIN_ID_QAIRT;
        String unit = placement.equals("gpu") ? "gpu" : placement.equals("cpu") ? "cpu" : "npu";
        ModelConfig cfg = new ModelConfig();
        cfg.setNCtx(ctx);
        cfg.setNThreads(Math.max(2, Math.min(6, Runtime.getRuntime().availableProcessors() / 2)));
        if (gguf) cfg.setNGpuLayers(unit.equals("cpu") ? 0 : 999);
        String model = path;
        File f = new File(path);
        if (f.isDirectory()) {
            File gc = new File(f, "genie_config.json");
            if (gc.exists()) model = gc.getAbsolutePath();
        }
        handle = llm.create(new LlmCreateInput(model, tokenizer == null ? "" : tokenizer, cfg, runtime, unit));
        if (handle == 0) throw new Exception("GenieX no pudo cargar el modelo (" + runtime + " en " + unit.toUpperCase() + ")");
    }

    public void stop() {
        stop = true;
        if (handle != 0) llm.stopStream(handle);
    }

    public void close() {
        if (handle != 0) llm.destroy(handle);
        handle = 0;
    }

    public void run(JSONObject req, final Engines.Sink sink) throws Exception {
        stop = false;
        llm.reset(handle);
        String prompt = req.optString("prompt", "");
        if (prompt.isEmpty()) {  // GenieX aplica la plantilla de chat del modelo
            JSONArray hist = req.optJSONArray("history");
            int n = (hist == null ? 0 : hist.length()) + 2;
            ChatMessage[] msgs = new ChatMessage[n];
            int i = 0;
            msgs[i++] = new ChatMessage("system", req.optString("system", ""), null, null, null);
            for (int j = 0; hist != null && j < hist.length(); j++) {
                JSONObject m = hist.getJSONObject(j);
                msgs[i++] = new ChatMessage(m.optString("role"), m.optString("content"), null, null, null);
            }
            msgs[i] = new ChatMessage("user", req.optString("message", ""), null, null, null);
            prompt = llm.applyChatTemplate(handle, msgs, null, true, false, false).getFormattedText();
        }
        GenerationConfig gen = new GenerationConfig();
        gen.setMaxTokens(req.optInt("maxTokens", 1024));
        SamplerConfig s = new SamplerConfig();
        s.setTemperature((float) req.optDouble("temperature", 0.6));
        s.setTopP(0.95f);
        s.setTopK(40);
        gen.setSamplerConfig(s);
        llm.generate(handle, prompt, null, null, 0, gen, new LLMTokenCallback() {
            public boolean onToken(String piece) {
                return !stop && sink.text(piece);
            }

            public void onComplete(LlmGenerateResult r) {
            }
        });
    }
}
