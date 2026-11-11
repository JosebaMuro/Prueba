package com.studioia.local;

/** Puente JNI con LiteRT-LM (liblitert-lm.so de Google), ver jni/litertlm_jni.c. */
final class LiteRt {
    private static boolean loaded;

    static synchronized void load() {
        if (!loaded) {
            System.loadLibrary("litert-lm");
            System.loadLibrary("litertlm_jni");
            loaded = true;
        }
    }

    interface Listener {
        /** text: trozo (JSON de mensaje parcial); last: fin de la respuesta; error: no nulo si falló. */
        void onChunk(byte[] text, boolean last, byte[] error);
    }

    static native byte[] lastError();

    /** backend: "cpu", "gpu" o "npu". Devuelve 0 si no se pudo crear (ver lastError). */
    static native long engineCreate(String model, String backend, int maxTokens, String cacheDir, String dispatchDir);

    static native void engineDelete(long engine);

    /** messages: JSON con el historial previo [{"role","content"}], o "[]". */
    static native long conversationCreate(long engine, String system, String messages);

    static native void conversationDelete(long conv);

    static native void cancel(long conv);

    /** message: JSON {"role":"user","content":"..."}. 0 = arrancó; el texto llega al listener. */
    static native int send(long conv, String message, Listener listener);

    static String error() {
        try {
            byte[] b = lastError();
            return b == null ? "" : new String(b, "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }
}
