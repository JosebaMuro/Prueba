// Lanzador de Studio IA Local.
//
// Arranca (o instala) Ollama, levanta el backend Python portátil incluido en ./runtime,
// espera a que responda y abre la interfaz en el navegador. Con LAN=true en studio.env
// expone el estudio en la red local protegido por token y muestra los datos para la app Android.
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	qrterminal "github.com/mdp/qrterminal/v3"
)

const version = "1.2.0"

func main() {
	noBrowser := flag.Bool("no-browser", false, "no abrir el navegador")
	flag.Parse()

	exe, _ := os.Executable()
	root := filepath.Dir(exe)
	if v := os.Getenv("STUDIO_ROOT"); v != "" {
		root = v
	}
	fmt.Printf("\n  Studio IA Local %s\n  Carpeta: %s\n\n", version, root)

	env := loadEnv(filepath.Join(root, "studio.env"))
	set := func(k, v string) {
		if env[k] == "" {
			env[k] = v
		}
	}
	set("PORT", "8765")
	set("OLLAMA_URL", "http://127.0.0.1:11434")
	set("DATA_DIR", filepath.Join(root, "data"))
	set("WORKSPACE_DIR", filepath.Join(root, "workspace"))
	set("FRONTEND_DIR", filepath.Join(root, "app", "frontend"))
	set("HF_HOME", filepath.Join(root, "data", "modelos-voz"))
	if strings.EqualFold(env["LAN"], "true") {
		set("HOST", "0.0.0.0")
		if env["ACCESS_TOKEN"] == "" {
			env["ACCESS_TOKEN"] = persistentToken(filepath.Join(env["DATA_DIR"], "token.txt"))
		}
	}
	set("HOST", "127.0.0.1")
	if env["PIPER_VOICE"] == "" {
		if m, _ := filepath.Glob(filepath.Join(root, "voices", "*.onnx")); len(m) > 0 {
			env["PIPER_VOICE"] = m[0]
		}
	}

	ensureOllama(env["OLLAMA_URL"])

	py := filepath.Join(root, "runtime", "python.exe")
	if runtime.GOOS != "windows" {
		py = filepath.Join(root, "runtime", "bin", "python3")
	}
	cmd := exec.Command(py, "-m", "app.main")
	cmd.Dir = filepath.Join(root, "app", "backend")
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Env = append(cmd.Env, "PYTHONUTF8=1", "PYTHONDONTWRITEBYTECODE=1")
	if err := cmd.Start(); err != nil {
		fatal("No se pudo iniciar el motor del estudio (%s): %v", py, err)
	}

	local := "http://127.0.0.1:" + env["PORT"]
	go func() {
		if !waitHTTP(local+"/api/ping", 120*time.Second) {
			fmt.Println("  ⚠ El servidor tarda en responder; revisa los mensajes de arriba.")
			return
		}
		fmt.Printf("\n  ✔ Estudio listo en %s\n", local)
		if env["HOST"] == "0.0.0.0" {
			printLAN(env["PORT"], env["ACCESS_TOKEN"])
		}
		fmt.Println("\n  Cierra esta ventana o pulsa Ctrl+C para detener el estudio.")
		if !*noBrowser {
			openBrowser(local)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-sig:
		_ = cmd.Process.Kill()
	case err := <-done:
		if err != nil {
			fatal("El motor del estudio se detuvo: %v", err)
		}
	}
}

func loadEnv(path string) map[string]string {
	env := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return env
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if k, v, ok := strings.Cut(line, "="); ok {
			env[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"'`)
		}
	}
	return env
}

func persistentToken(path string) string {
	if b, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(b))) >= 8 {
		return strings.TrimSpace(string(b))
	}
	buf := make([]byte, 4)
	_, _ = rand.Read(buf)
	tok := strings.ToUpper(hex.EncodeToString(buf))
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	_ = os.WriteFile(path, []byte(tok), 0o600)
	return tok
}

func waitHTTP(url string, timeout time.Duration) bool {
	c := http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if r, err := c.Get(url); err == nil {
			r.Body.Close()
			if r.StatusCode < 500 {
				return true
			}
		}
		time.Sleep(700 * time.Millisecond)
	}
	return false
}

// Ajustes de rendimiento de Ollama: flash attention, caché KV cuantizada (la mitad de memoria),
// modelos residentes y una sola petición en paralelo (un usuario: no reserva caché para 4).
var ollamaTuning = map[string]string{"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
	"OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_MAX_LOADED_MODELS": "3", "OLLAMA_NUM_PARALLEL": "1"}

func ensureOllama(base string) {
	if waitHTTP(base+"/api/version", 2*time.Second) {
		fmt.Println("  ✔ Ollama en marcha")
		// Ollama ya estaba abierto (p. ej. la app de la bandeja de Windows) y no hereda nuestras variables:
		// las dejamos guardadas para el usuario, así se aplican la próxima vez que Ollama arranque.
		if persistTuning() {
			fmt.Println("  ℹ Ajustes de rendimiento de Ollama guardados: se aplican al reiniciar Ollama")
		}
		return
	}
	exe := findOllama()
	if exe == "" {
		if askYesNo("Studio IA Local", "Ollama (el motor de modelos) no está instalado.\n\n¿Quieres instalarlo ahora? (usa winget, requiere Internet una vez)") && installOllama() {
			exe = findOllama()
		}
	}
	if exe == "" {
		fmt.Println("  ✖ Ollama no encontrado. Instálalo desde https://ollama.com/download y vuelve a abrir el estudio.")
		openBrowser("https://ollama.com/download")
		return
	}
	fmt.Println("  … iniciando Ollama")
	c := exec.Command(exe, "serve")
	c.Env = os.Environ()
	persistTuning()
	for k, v := range ollamaTuning {
		if os.Getenv(k) == "" {
			c.Env = append(c.Env, k+"="+v)
		}
	}
	hideWindow(c)
	if err := c.Start(); err != nil {
		fmt.Println("  ✖ No se pudo iniciar Ollama:", err)
		return
	}
	if waitHTTP(base+"/api/version", 30*time.Second) {
		fmt.Println("  ✔ Ollama en marcha")
	}
}

func findOllama() string {
	if p, err := exec.LookPath("ollama"); err == nil {
		return p
	}
	for _, p := range []string{
		filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "Ollama", "ollama.exe"),
		filepath.Join(os.Getenv("ProgramFiles"), "Ollama", "ollama.exe"),
		"/usr/local/bin/ollama", "/usr/bin/ollama",
	} {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

func printLAN(port, token string) {
	ips := lanIPs()
	fmt.Println("\n  📱 Acceso desde la red local (app Android o navegador del móvil):")
	for _, ip := range ips {
		fmt.Printf("     Servidor: http://%s:%s   Token: %s\n", ip, port, token)
	}
	if len(ips) > 0 {
		url := fmt.Sprintf("http://%s:%s/?token=%s", ips[0], port, token)
		fmt.Println("\n  Escanea con la cámara del móvil para abrirlo en el navegador:")
		qrterminal.GenerateWithConfig(url, qrterminal.Config{
			Level: qrterminal.L, Writer: os.Stdout, HalfBlocks: true,
			BlackChar: qrterminal.BLACK_BLACK, WhiteChar: qrterminal.WHITE_WHITE,
			BlackWhiteChar: qrterminal.BLACK_WHITE, WhiteBlackChar: qrterminal.WHITE_BLACK, QuietZone: 2,
		})
	}
}

func lanIPs() []string {
	var out []string
	ifaces, _ := net.Interfaces()
	for _, i := range ifaces {
		if i.Flags&net.FlagUp == 0 || i.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := i.Addrs()
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil && n.IP.IsPrivate() {
				out = append(out, n.IP.String())
			}
		}
	}
	return out
}

func fatal(format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	fmt.Println("\n  ✖ " + msg)
	showError("Studio IA Local", msg)
	fmt.Println("\n  Pulsa Intro para salir…")
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	os.Exit(1)
}
