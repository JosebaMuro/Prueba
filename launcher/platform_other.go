//go:build !windows

package main

import (
	"fmt"
	"os/exec"
	"runtime"
)

func askYesNo(title, text string) bool { fmt.Println("  " + text); return false }
func showError(title, text string)      {}
func hideWindow(c *exec.Cmd)            {}
func installOllama() bool {
	fmt.Println("  Instala Ollama con: curl -fsSL https://ollama.com/install.sh | sh")
	return false
}

func openBrowser(url string) {
	cmd := "xdg-open"
	if runtime.GOOS == "darwin" {
		cmd = "open"
	}
	_ = exec.Command(cmd, url).Start()
}

func persistTuning() bool { return false }
