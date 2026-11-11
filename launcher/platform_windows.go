//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
	"unsafe"
)

var (
	user32      = syscall.NewLazyDLL("user32.dll")
	messageBoxW = user32.NewProc("MessageBoxW")
)

func msgBox(title, text string, flags uintptr) int {
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(text)
	r, _, _ := messageBoxW.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), flags)
	return int(r)
}

func askYesNo(title, text string) bool { return msgBox(title, text, 0x04|0x20) == 6 } // MB_YESNO|MB_ICONQUESTION -> IDYES
func showError(title, text string)      { msgBox(title, text, 0x10) }

func openBrowser(url string) {
	_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
}

func hideWindow(c *exec.Cmd) {
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}

func installOllama() bool {
	fmt.Println("  … instalando Ollama con winget (puede tardar unos minutos)")
	c := exec.Command("winget", "install", "-e", "--id", "Ollama.Ollama",
		"--accept-source-agreements", "--accept-package-agreements")
	c.Stdout, c.Stderr, c.Stdin = os.Stdout, os.Stderr, os.Stdin
	if err := c.Run(); err != nil {
		fmt.Println("  ✖ winget falló:", err)
		return false
	}
	return true
}

// persistTuning guarda los ajustes de rendimiento de Ollama como variables de entorno del usuario
// (solo las que no existan ya), igual que haría "setx". Devuelve true si guardó alguna.
func persistTuning() bool {
	saved := false
	for k, v := range ollamaTuning {
		if os.Getenv(k) != "" {
			continue
		}
		if out, err := exec.Command("reg", "query", `HKCU\Environment`, "/v", k).CombinedOutput(); err == nil && len(out) > 0 {
			continue
		}
		c := exec.Command("setx", k, v)
		hideWindow(c)
		if c.Run() == nil {
			saved = true
		}
	}
	return saved
}
