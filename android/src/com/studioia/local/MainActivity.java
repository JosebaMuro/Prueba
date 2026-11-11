package com.studioia.local;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ContentValues;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.text.InputType;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Cliente Android de Studio IA Local.
 *
 * El estudio (modelos, agentes, memoria y workspace) corre en tu PC; esta app se conecta a él
 * por la red local, de modo que nada sale de tu casa. Añade lo que el navegador no puede hacer
 * sobre HTTP en la LAN: micrófono nativo, cámara, descargas y descubrimiento automático.
 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 10;
    private static final int REQ_PERM_AUDIO = 20;
    private static final int REQ_PERM_CAMERA = 21;
    private static final int DEFAULT_PORT = 8765;

    private SharedPreferences prefs;
    private WebView web;
    private LocalStudio local;
    private boolean webIsLocal = false;
    private ValueCallback<Uri[]> fileCallback;
    private Uri cameraUri;
    private MediaRecorder recorder;
    private File audioFile;
    private boolean recording = false;
    private final Handler ui = new Handler(Looper.getMainLooper());

    // ------------------------------------------------------------------ ciclo de vida

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("studio", MODE_PRIVATE);
        getWindow().setStatusBarColor(Color.parseColor("#0f1115"));
        getWindow().setNavigationBarColor(Color.parseColor("#0f1115"));
        String server = prefs.getString("server", "");
        if (server.isEmpty() && !isLocalMode()) {
            showSetup(null);
        } else {
            showStudio();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.getParent() != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        if (web != null && web.getParent() != null) {
            new AlertDialog.Builder(this)
                    .setTitle("Studio IA")
                    .setItems(new String[]{"Cambiar de modo o servidor", "Recargar", "Salir"}, new DialogInterface.OnClickListener() {
                        public void onClick(DialogInterface d, int which) {
                            if (which == 0) showSetup(null);
                            else if (which == 1) web.reload();
                            else finish();
                        }
                    }).show();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        stopRecorder(false);
        if (local != null) local.destroy();
        super.onDestroy();
    }

    // ------------------------------------------------------------------ pantalla de conexión

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private EditText field(String hint, String value, int type) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setText(value);
        e.setInputType(type);
        e.setSingleLine(true);
        e.setTextColor(Color.WHITE);
        e.setHintTextColor(Color.parseColor("#8b93a7"));
        return e;
    }

    private Button button(String text, boolean primary) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(10));
        bg.setColor(Color.parseColor(primary ? "#8b74ff" : "#1e222b"));
        b.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48));
        lp.topMargin = dp(12);
        b.setLayoutParams(lp);
        return b;
    }

    private boolean isLocalMode() {
        return "local".equals(prefs.getString("mode", ""));
    }

    void openSetup() {
        showSetup(null);
    }

    private void showSetup(String error) {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#0f1115"));
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(24), dp(48), dp(24), dp(24));
        scroll.addView(col);

        TextView title = new TextView(this);
        title.setText("Studio IA Local");
        title.setTextSize(28);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(Color.WHITE);
        col.addView(title);

        TextView sub = new TextView(this);
        sub.setText("Elige cómo quieres usar el estudio.");
        sub.setTextColor(Color.parseColor("#8b93a7"));
        sub.setPadding(0, dp(8), 0, dp(8));
        col.addView(sub);

        Button standalone = button("📱  Usar solo este móvil (autónomo)", true);
        standalone.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                prefs.edit().putString("mode", "local").apply();
                showStudio();
            }
        });
        col.addView(standalone);
        TextView subLocal = new TextView(this);
        subLocal.setText("Los modelos se descargan una vez y se ejecutan en el teléfono. No necesita PC ni conexión. Recomendado: 8 GB de RAM o más.");
        subLocal.setTextColor(Color.parseColor("#8b93a7"));
        subLocal.setTextSize(13);
        subLocal.setPadding(0, dp(8), 0, dp(28));
        col.addView(subLocal);

        TextView subPc = new TextView(this);
        subPc.setText("O conecta con el estudio de tu PC (más potente). En el PC pon LAN=true en studio.env y reinicia StudioIA.exe: la ventana muestra la dirección y el token.");
        subPc.setTextColor(Color.parseColor("#8b93a7"));
        subPc.setTextSize(13);
        subPc.setPadding(0, 0, 0, dp(8));
        col.addView(subPc);

        if (error != null) {
            TextView err = new TextView(this);
            err.setText(error);
            err.setTextColor(Color.parseColor("#ff6b6b"));
            err.setPadding(0, 0, 0, dp(12));
            col.addView(err);
        }

        final EditText server = field("Servidor, p. ej. 192.168.1.20:8765", prefs.getString("server", ""), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        final EditText token = field("Token de acceso", prefs.getString("token", ""), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        col.addView(server);
        col.addView(token);

        final ProgressBar progress = new ProgressBar(this);
        progress.setVisibility(View.GONE);

        Button connect = button("Conectar con el PC", false);
        connect.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String s = normalize(server.getText().toString());
                if (s.isEmpty()) {
                    toast("Escribe la dirección del servidor");
                    return;
                }
                prefs.edit().putString("mode", "remote").putString("server", s).putString("token", token.getText().toString().trim()).apply();
                showStudio();
            }
        });
        col.addView(connect);

        Button scan = button("Buscar el estudio en mi red", false);
        scan.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                progress.setVisibility(View.VISIBLE);
                discover(new Found() {
                    public void done(final List<String> hosts) {
                        progress.setVisibility(View.GONE);
                        if (hosts.isEmpty()) {
                            toast("No se encontró ningún estudio. ¿Está LAN=true y el PC en la misma Wi-Fi?");
                            return;
                        }
                        final String[] items = hosts.toArray(new String[0]);
                        new AlertDialog.Builder(MainActivity.this).setTitle("Estudios encontrados")
                                .setItems(items, new DialogInterface.OnClickListener() {
                                    public void onClick(DialogInterface d, int which) {
                                        server.setText(items[which]);
                                        token.requestFocus();
                                    }
                                }).show();
                    }
                });
            }
        });
        col.addView(scan);
        col.addView(progress);

        TextView foot = new TextView(this);
        foot.setText("\nTodo el procesamiento ocurre en tu PC. La app solo muestra la interfaz y envía tu voz, imágenes y mensajes por tu red local.");
        foot.setTextColor(Color.parseColor("#667085"));
        foot.setTextSize(12);
        col.addView(foot);

        setContentView(scroll);
    }

    private static String normalize(String s) {
        s = s.trim();
        if (s.isEmpty()) return s;
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://" + s;
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        if (s.indexOf(':', s.indexOf("//") + 2) < 0) s = s + ":" + DEFAULT_PORT;
        return s;
    }

    // ------------------------------------------------------------------ descubrimiento en la LAN

    interface Found {
        void done(List<String> hosts);
    }

    private void discover(final Found cb) {
        new Thread(new Runnable() {
            public void run() {
                final List<String> found = Collections.synchronizedList(new ArrayList<String>());
                ExecutorService pool = Executors.newFixedThreadPool(48);
                try {
                    Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
                    while (ifs != null && ifs.hasMoreElements()) {
                        NetworkInterface ni = ifs.nextElement();
                        if (!ni.isUp() || ni.isLoopback()) continue;
                        Enumeration<InetAddress> addrs = ni.getInetAddresses();
                        while (addrs.hasMoreElements()) {
                            InetAddress a = addrs.nextElement();
                            if (!(a instanceof Inet4Address) || !a.isSiteLocalAddress()) continue;
                            String ip = a.getHostAddress();
                            String prefix = ip.substring(0, ip.lastIndexOf('.') + 1);
                            for (int i = 1; i < 255; i++) {
                                final String host = prefix + i;
                                pool.execute(new Runnable() {
                                    public void run() {
                                        if (ping("http://" + host + ":" + DEFAULT_PORT)) found.add(host + ":" + DEFAULT_PORT);
                                    }
                                });
                            }
                        }
                    }
                    pool.shutdown();
                    pool.awaitTermination(20, TimeUnit.SECONDS);
                } catch (Exception ignored) {
                }
                ui.post(new Runnable() {
                    public void run() {
                        cb.done(new ArrayList<String>(found));
                    }
                });
            }
        }).start();
    }

    private static boolean ping(String base) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/ping").openConnection();
            c.setConnectTimeout(400);
            c.setReadTimeout(800);
            if (c.getResponseCode() != 200) return false;
            InputStream in = c.getInputStream();
            byte[] buf = new byte[256];
            int n = in.read(buf);
            return n > 0 && new String(buf, 0, n, "UTF-8").contains("studio-ia-local");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ------------------------------------------------------------------ WebView del estudio

    private void showStudio() {
        final boolean localMode = isLocalMode();
        final String server = localMode ? LocalStudio.ORIGIN : prefs.getString("server", "");
        String token = localMode ? "" : prefs.getString("token", "");
        if (web != null && webIsLocal != localMode) {  // cambio de modo: WebView nuevo con otro puente
            if (web.getParent() != null) ((ViewGroup) web.getParent()).removeView(web);
            web.destroy();
            web = null;
            if (local != null) local.destroy();
            local = null;
        }
        if (web == null) {
            web = new WebView(this);
            webIsLocal = localMode;
            WebSettings s = web.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setMediaPlaybackRequiresUserGesture(false);
            s.setAllowFileAccess(false);
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
            s.setUserAgentString(s.getUserAgentString() + " StudioIA-Android/1.0");
            web.setBackgroundColor(Color.parseColor("#0f1115"));
            if (localMode) {
                local = new LocalStudio(this, web);
                web.addJavascriptInterface(local.new Bridge(), "AndroidBridge");
            } else {
                web.addJavascriptInterface(new Bridge(), "AndroidBridge");
            }
            CookieManager.getInstance().setAcceptCookie(true);
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

            web.setWebViewClient(new WebViewClient() {
                @Override
                public android.webkit.WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                    return local != null ? local.intercept(req.getUrl().toString()) : null;
                }

                @Override
                public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                    if (req.isForMainFrame() && !webIsLocal) {
                        showSetup("No se pudo conectar con " + prefs.getString("server", "") + " (" + err.getDescription() + "). Comprueba que el PC está encendido, StudioIA.exe abierto con LAN=true y que estáis en la misma red.");
                    }
                }

                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                    Uri u = req.getUrl();
                    if (u.toString().startsWith(webIsLocal ? LocalStudio.ORIGIN : prefs.getString("server", "x"))) return false;
                    startActivity(new Intent(Intent.ACTION_VIEW, u));  // enlaces externos al navegador
                    return true;
                }
            });

            web.setWebChromeClient(new WebChromeClient() {
                @Override
                public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                    if (fileCallback != null) fileCallback.onReceiveValue(null);
                    fileCallback = callback;
                    openChooser(params);
                    return true;
                }

                @Override
                public void onPermissionRequest(PermissionRequest request) {
                    request.grant(request.getResources());
                }
            });

            web.setDownloadListener(new DownloadListener() {
                public void onDownloadStart(String url, String ua, String disposition, String mime, long len) {
                    try {
                        DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
                        r.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
                        String name = URLUtil.guessFileName(url, disposition, mime);
                        r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                        r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                        ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
                        toast("Descargando " + name);
                    } catch (Exception e) {
                        toast("No se pudo descargar: " + e.getMessage());
                    }
                }
            });
        }
        if (web.getParent() != null) ((ViewGroup) web.getParent()).removeView(web);
        setContentView(web);
        String url = localMode ? LocalStudio.ORIGIN : server + "/";
        if (!token.isEmpty()) url += "?token=" + Uri.encode(token);
        web.loadUrl(url);
    }

    // ------------------------------------------------------------------ cámara y archivos

    private void openChooser(WebChromeClient.FileChooserParams params) {
        Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType("*/*");
        String[] accept = params != null ? params.getAcceptTypes() : null;
        boolean images = false;
        if (accept != null) {
            for (String a : accept) if (a != null && a.startsWith("image")) images = true;
        }
        pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        Intent chooser = Intent.createChooser(pick, "Adjuntar");
        if (images && hasPerm(Manifest.permission.CAMERA)) {
            try {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Images.Media.DISPLAY_NAME, "studio_" + System.currentTimeMillis() + ".jpg");
                cv.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
                cameraUri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv);
                Intent cam = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
                cam.putExtra(MediaStore.EXTRA_OUTPUT, cameraUri);
                chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{cam});
            } catch (Exception e) {
                cameraUri = null;
            }
        } else if (images) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_PERM_CAMERA);
        }
        startActivityForResult(chooser, REQ_FILE);
    }

    @Override
    protected void onActivityResult(int req, int result, Intent data) {
        super.onActivityResult(req, result, data);
        if (req != REQ_FILE || fileCallback == null) return;
        Uri[] out = null;
        if (result == RESULT_OK) {
            if (data != null && data.getClipData() != null) {
                int n = data.getClipData().getItemCount();
                out = new Uri[n];
                for (int i = 0; i < n; i++) out[i] = data.getClipData().getItemAt(i).getUri();
            } else if (data != null && data.getData() != null) {
                out = new Uri[]{data.getData()};
            } else if (cameraUri != null) {
                out = new Uri[]{cameraUri};
            }
        }
        fileCallback.onReceiveValue(out);
        fileCallback = null;
    }

    // ------------------------------------------------------------------ micrófono nativo

    private boolean hasPerm(String p) {
        return checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        if (req == REQ_PERM_AUDIO && res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
            toast("Micrófono permitido. Pulsa de nuevo 🎙️ para grabar.");
        }
    }

    private boolean startRecorder() {
        if (!hasPerm(Manifest.permission.RECORD_AUDIO)) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_PERM_AUDIO);
            return false;
        }
        try {
            audioFile = new File(getCacheDir(), "voz.m4a");
            recorder = Build.VERSION.SDK_INT >= 31 ? new MediaRecorder(this) : new MediaRecorder();
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC);
            recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            recorder.setAudioSamplingRate(16000);
            recorder.setAudioEncodingBitRate(64000);
            recorder.setOutputFile(audioFile.getAbsolutePath());
            recorder.prepare();
            recorder.start();
            recording = true;
            return true;
        } catch (Exception e) {
            toast("No se pudo grabar: " + e.getMessage());
            recorder = null;
            return false;
        }
    }

    private void stopRecorder(boolean deliver) {
        if (recorder == null) return;
        try {
            recorder.stop();
        } catch (Exception ignored) {
            deliver = false;
        }
        recorder.release();
        recorder = null;
        recording = false;
        if (!deliver) return;
        try {
            FileInputStream in = new FileInputStream(audioFile);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            in.close();
            final String b64 = Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP);
            web.evaluateJavascript("window.onAndroidAudio && window.onAndroidAudio('" + b64 + "','audio/mp4')", null);
        } catch (Exception e) {
            toast("Error leyendo el audio: " + e.getMessage());
        }
    }

    /** API expuesta a la interfaz web como window.AndroidBridge. */
    public class Bridge {
        @JavascriptInterface
        public boolean startRecording() {
            final boolean[] ok = {false};
            final Object lock = new Object();
            synchronized (lock) {
                ui.post(new Runnable() {
                    public void run() {
                        synchronized (lock) {
                            ok[0] = startRecorder();
                            lock.notifyAll();
                        }
                    }
                });
                try {
                    lock.wait(3000);
                } catch (InterruptedException ignored) {
                }
            }
            return ok[0];
        }

        @JavascriptInterface
        public void stopRecording() {
            ui.post(new Runnable() {
                public void run() {
                    stopRecorder(true);
                }
            });
        }

        @JavascriptInterface
        public boolean isRecording() {
            return recording;
        }

        @JavascriptInterface
        public void changeServer() {
            ui.post(new Runnable() {
                public void run() {
                    showSetup(null);
                }
            });
        }
    }

    private void toast(String msg) {
        Toast t = Toast.makeText(this, msg, Toast.LENGTH_LONG);
        t.setGravity(Gravity.BOTTOM, 0, dp(80));
        t.show();
    }
}
