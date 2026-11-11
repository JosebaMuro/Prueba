/*
 * Puente JNI mínimo entre Java y la API C de LiteRT-LM (liblitert-lm.so, de Google).
 * No usa la biblioteca C: solo JNI y las funciones de liblitert-lm, así se compila con zig sin el NDK.
 * Lo usa com.studioia.local.LiteRt. Compilar con android/build_android.sh.
 */
#include <jni.h>
#include <stdbool.h>
#include <stddef.h>

typedef void (*litert_cb)(void *user, void *chunk);
extern int litert_lm_engine_settings_create(const char *, const char *, const char *, const char *, void **);
extern int litert_lm_engine_settings_set_max_num_tokens(void *, int);
extern int litert_lm_engine_settings_set_cache_dir(void *, const char *);
extern int litert_lm_engine_settings_set_litert_dispatch_lib_dir(void *, const char *);
extern void litert_lm_engine_settings_delete(void *);
extern int litert_lm_engine_create(void *, void **);
extern void litert_lm_engine_delete(void *);
extern int litert_lm_conversation_config_create(void **);
extern void litert_lm_conversation_config_delete(void *);
extern int litert_lm_conversation_config_set_system_message(void *, const char *);
extern int litert_lm_conversation_config_set_messages(void *, const char *);
extern int litert_lm_conversation_create(void *, void *, void **);
extern void litert_lm_conversation_delete(void *);
extern int litert_lm_conversation_send_message_stream(void *, const char *, const char *, void *, litert_cb, void *);
extern int litert_lm_conversation_cancel_process(void *);
extern int litert_lm_stream_chunk_get_text(void *, const char **);
extern int litert_lm_stream_chunk_is_final(void *, bool *);
extern int litert_lm_stream_chunk_get_error(void *, const char **);
extern const char *litert_lm_get_last_error_message(void);

static JavaVM *g_vm;
static jmethodID g_on_chunk;

JNIEXPORT jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    g_vm = vm;
    return JNI_VERSION_1_6;
}

static jsize len(const char *s) {
    jsize n = 0;
    if (s) while (s[n]) n++;
    return n;
}

/* Bytes UTF-8 de verdad (NewStringUTF espera UTF-8 "modificado" y falla con emojis). */
static jbyteArray bytes(JNIEnv *env, const char *s) {
    if (!s) return NULL;
    jsize n = len(s);
    jbyteArray a = (*env)->NewByteArray(env, n);
    if (a && n) (*env)->SetByteArrayRegion(env, a, 0, n, (const jbyte *)s);
    return a;
}

#define UTF(js) ((js) ? (*env)->GetStringUTFChars(env, (js), NULL) : NULL)
#define FREE(js, c) do { if (js) (*env)->ReleaseStringUTFChars(env, (js), (c)); } while (0)

JNIEXPORT jbyteArray JNICALL Java_com_studioia_local_LiteRt_lastError(JNIEnv *env, jclass c) {
    (void)c;
    return bytes(env, litert_lm_get_last_error_message());
}

JNIEXPORT jlong JNICALL Java_com_studioia_local_LiteRt_engineCreate(JNIEnv *env, jclass c, jstring model,
        jstring backend, jint max_tokens, jstring cache_dir, jstring dispatch_dir) {
    (void)c;
    const char *m = UTF(model), *b = UTF(backend), *cd = UTF(cache_dir), *dd = UTF(dispatch_dir);
    void *settings = NULL, *engine = NULL;
    if (litert_lm_engine_settings_create(m, b, NULL, NULL, &settings) == 0 && settings) {
        if (max_tokens > 0) litert_lm_engine_settings_set_max_num_tokens(settings, max_tokens);
        if (cd && cd[0]) litert_lm_engine_settings_set_cache_dir(settings, cd);
        if (dd && dd[0]) litert_lm_engine_settings_set_litert_dispatch_lib_dir(settings, dd);
        if (litert_lm_engine_create(settings, &engine) != 0) engine = NULL;
        litert_lm_engine_settings_delete(settings);
    }
    FREE(model, m); FREE(backend, b); FREE(cache_dir, cd); FREE(dispatch_dir, dd);
    return (jlong)(size_t)engine;
}

JNIEXPORT void JNICALL Java_com_studioia_local_LiteRt_engineDelete(JNIEnv *env, jclass c, jlong engine) {
    (void)env; (void)c;
    if (engine) litert_lm_engine_delete((void *)(size_t)engine);
}

JNIEXPORT jlong JNICALL Java_com_studioia_local_LiteRt_conversationCreate(JNIEnv *env, jclass c, jlong engine,
        jstring system, jstring messages) {
    (void)c;
    void *cfg = NULL, *conv = NULL;
    if (litert_lm_conversation_config_create(&cfg) != 0 || !cfg) return 0;
    const char *s = UTF(system), *msgs = UTF(messages);
    if (s && s[0]) litert_lm_conversation_config_set_system_message(cfg, s);
    if (msgs && msgs[0] && !(msgs[0] == '[' && msgs[1] == ']')) litert_lm_conversation_config_set_messages(cfg, msgs);
    if (litert_lm_conversation_create((void *)(size_t)engine, cfg, &conv) != 0) conv = NULL;
    litert_lm_conversation_config_delete(cfg);
    FREE(system, s); FREE(messages, msgs);
    return (jlong)(size_t)conv;
}

JNIEXPORT void JNICALL Java_com_studioia_local_LiteRt_conversationDelete(JNIEnv *env, jclass c, jlong conv) {
    (void)env; (void)c;
    if (conv) litert_lm_conversation_delete((void *)(size_t)conv);
}

JNIEXPORT void JNICALL Java_com_studioia_local_LiteRt_cancel(JNIEnv *env, jclass c, jlong conv) {
    (void)env; (void)c;
    if (conv) litert_lm_conversation_cancel_process((void *)(size_t)conv);
}

/* Se llama desde un hilo de LiteRT-LM: se engancha a la JVM y entrega el trozo al listener de Java. */
static void on_chunk(void *user, void *chunk) {
    JNIEnv *env = NULL;
    int attached = 0;
    if ((*g_vm)->GetEnv(g_vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK) {
        if ((*g_vm)->AttachCurrentThread(g_vm, (void **)&env, NULL) != JNI_OK) return;
        attached = 1;
    }
    const char *text = NULL, *err = NULL;
    bool final = false;
    litert_lm_stream_chunk_get_error(chunk, &err);
    litert_lm_stream_chunk_get_text(chunk, &text);
    litert_lm_stream_chunk_is_final(chunk, &final);
    jobject listener = (jobject)user;
    jbyteArray jt = bytes(env, text), je = (err && err[0]) ? bytes(env, err) : NULL;
    (*env)->CallVoidMethod(env, listener, g_on_chunk, jt, (jboolean)(final || je != NULL), je);
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    if (jt) (*env)->DeleteLocalRef(env, jt);
    if (je) (*env)->DeleteLocalRef(env, je);
    if (final || (err && err[0])) (*env)->DeleteGlobalRef(env, listener);
    if (attached) (*g_vm)->DetachCurrentThread(g_vm);
}

/* Envía un mensaje (JSON {"role","content"}) y devuelve 0 si la generación arrancó; el texto llega por el listener. */
JNIEXPORT jint JNICALL Java_com_studioia_local_LiteRt_send(JNIEnv *env, jclass c, jlong conv, jstring message,
        jobject listener) {
    (void)c;
    if (!g_on_chunk) {
        jclass lc = (*env)->GetObjectClass(env, listener);
        g_on_chunk = (*env)->GetMethodID(env, lc, "onChunk", "([BZ[B)V");
        if (!g_on_chunk) return -2;
    }
    jobject ref = (*env)->NewGlobalRef(env, listener);
    const char *m = UTF(message);
    int rc = litert_lm_conversation_send_message_stream((void *)(size_t)conv, m, "{}", NULL, on_chunk, ref);
    FREE(message, m);
    if (rc != 0) (*env)->DeleteGlobalRef(env, ref);
    return rc;
}
