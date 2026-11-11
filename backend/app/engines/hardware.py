"""Detección del hardware: CPU, RAM, GPUs (con su VRAM) y NPUs.

Sin dependencias: registro de Windows, /proc y /sys en Linux, nvidia-smi si existe.
El resultado se cachea; refresh() lo vuelve a leer (p. ej. tras instalar un motor).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

GB = 1024 ** 3


def _vendor(name: str) -> str:
    n = name.lower()
    for key, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("rtx", "nvidia"), ("quadro", "nvidia"),
                        ("radeon", "amd"), ("amd", "amd"), ("intel", "intel"), ("arc ", "intel"), ("ryzen", "amd"),
                        ("adreno", "qualcomm"), ("qualcomm", "qualcomm"), ("snapdragon", "qualcomm"),
                        ("hexagon", "qualcomm"), ("apple", "apple")):
        if key in n:
            return vendor
    return "other"


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: sin parpadeo de consola
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ram_gb() -> float:
    if sys.platform == "win32":
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMSTAT(); m.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / GB
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024 / GB
    except OSError:
        pass
    try:
        return int(_run(["sysctl", "-n", "hw.memsize"]).strip()) / GB
    except ValueError:
        return 0.0


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            return platform.processor()
    try:
        txt = Path("/proc/cpuinfo").read_text(errors="ignore")
        m = re.search(r"^(model name|Hardware|Processor)\s*:\s*(.+)$", txt, re.M)
        if m:
            return m.group(2).strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _windows_devices(class_guid: str) -> list[dict]:
    """Adaptadores de una clase de dispositivo de Windows (pantalla o aceleradores/NPU) con su memoria."""
    out = []
    try:
        import winreg
        base = rf"SYSTEM\CurrentControlSet\Control\Class\{class_guid}"
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        for i in range(64):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                k = winreg.OpenKey(root, sub)
                name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
            except OSError:
                continue
            mem = 0
            for val in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                try:
                    v = winreg.QueryValueEx(k, val)[0]
                    mem = int.from_bytes(v, "little") if isinstance(v, bytes) else int(v)
                    break
                except (OSError, ValueError, TypeError):
                    continue
            out.append({"name": name, "vram_gb": round(mem / GB, 1)})
    except OSError:
        pass
    return out


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe"
                                         if Path(r"C:\Windows\System32\nvidia-smi.exe").exists() else None)
    if not exe:
        return []
    gpus = []
    for line in _run([exe, "--query-gpu=name,memory.total,memory.free,compute_cap",
                      "--format=csv,noheader,nounits"]).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append({"name": parts[0], "vendor": "nvidia", "vram_gb": round(float(parts[1]) / 1024, 1),
                             "free_gb": round(float(parts[2]) / 1024, 1),
                             "compute_cap": parts[3] if len(parts) > 3 else ""})
            except ValueError:
                continue
    return gpus


def _linux_gpus() -> list[dict]:
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        dev = card / "device"
        try:
            vendor = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}.get((dev / "vendor").read_text().strip(), "other")
        except OSError:
            continue
        vram = 0.0
        try:
            vram = int((dev / "mem_info_vram_total").read_text()) / GB
        except (OSError, ValueError):
            pass
        gpus.append({"name": f"GPU {vendor.upper()} ({card.name})", "vendor": vendor, "vram_gb": round(vram, 1)})
    return gpus


@lru_cache(maxsize=1)
def detect() -> dict:
    cpu = _cpu_name()
    gpus = _nvidia()
    npus: list[dict] = []
    if sys.platform == "win32":
        seen = {g["name"] for g in gpus}
        for d in _windows_devices("{4d36e968-e325-11ce-bfc1-08002be10318}"):  # adaptadores de pantalla
            if d["name"] in seen or "basic display" in d["name"].lower() or "remote" in d["name"].lower():
                continue
            gpus.append({**d, "vendor": _vendor(d["name"])})
        for d in _windows_devices("{f01a9d53-3ff6-48d2-9f97-c8a7004be10c}"):  # aceleradores de cómputo (NPU)
            npus.append({"name": d["name"], "vendor": _vendor(d["name"])})
    elif sys.platform.startswith("linux"):
        if not gpus:
            gpus = _linux_gpus()
        else:
            gpus += [g for g in _linux_gpus() if g["vendor"] != "nvidia"]
        if Path("/dev/accel/accel0").exists():
            npus.append({"name": "NPU (accel)", "vendor": _vendor(cpu)})
    try:  # OpenVINO, si está instalado, ve las NPU de Intel aunque el registro no las nombre
        import importlib.util
        if importlib.util.find_spec("openvino") and not any(n["vendor"] == "intel" for n in npus):
            import openvino
            if "NPU" in openvino.Core().available_devices:
                npus.append({"name": "Intel NPU", "vendor": "intel"})
    except Exception:
        pass
    machine = platform.machine().lower()
    snapdragon = "snapdragon" in cpu.lower() or "qualcomm" in cpu.lower()
    if snapdragon:
        machine = "arm64"  # Python x64 emulado en Windows on ARM informa AMD64
    discrete = [g for g in gpus if g["vendor"] in ("nvidia", "amd") and g.get("vram_gb", 0) >= 2]
    return {
        "os": {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"),
        "arch": "arm64" if machine in ("arm64", "aarch64") else "x64",
        "cpu": cpu, "cpu_vendor": "qualcomm" if snapdragon else _vendor(cpu),
        "cores": os.cpu_count() or 1,
        "ram_gb": round(_ram_gb(), 1),
        "gpus": gpus,
        "npus": npus,
        "nvidia": any(g["vendor"] == "nvidia" for g in gpus),
        "best_vram_gb": max([g.get("vram_gb", 0) for g in discrete] or [0]),
        "has_gpu": bool(gpus),
        "has_npu": bool(npus),
    }


def refresh() -> dict:
    detect.cache_clear()
    return detect()
