from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LocalResourceSnapshot:
    cpu_name: str
    physical_cores: int
    logical_processors: int
    cpu_percent: float
    total_memory_mb: int
    available_memory_mb: int
    gpu_name: str = ""
    gpu_memory_total_mb: int = 0
    gpu_memory_used_mb: int = 0


def audit_local_resources() -> dict[str, Any]:
    snapshot = _snapshot()
    recommendation = _recommend(snapshot)
    return {
        "status": "ok",
        "schema": "local_resource_audit_v1",
        "snapshot": asdict(snapshot),
        "recommendation": recommendation,
        "policy": {
            "gpu_media": "固定 1 路；OCR/ASR 只有显式 GPU 档进入。",
            "llm_split": "交互模型调用保留 70%，后台模型调用最多 30%。",
            "media_cpu": "按 CPU 核心、当前负载、可用内存估算；长音频 ASR 单独限流。",
        },
    }


def _snapshot() -> LocalResourceSnapshot:
    cpu_info = _windows_cpu_info() if os.name == "nt" else {}
    memory_info = _windows_memory_info() if os.name == "nt" else {}
    gpu_info = _nvidia_smi_info()
    logical = _int(cpu_info.get("logical_processors"), os.cpu_count() or 1)
    physical = _int(cpu_info.get("physical_cores"), max(1, logical // 2))
    total_memory = _int(memory_info.get("total_mb"), 0)
    available_memory = _int(memory_info.get("available_mb"), 0)
    return LocalResourceSnapshot(
        cpu_name=str(cpu_info.get("name") or platform.processor() or platform.machine() or "unknown"),
        physical_cores=max(1, physical),
        logical_processors=max(1, logical),
        cpu_percent=_cpu_percent_sample(),
        total_memory_mb=max(0, total_memory),
        available_memory_mb=max(0, available_memory),
        gpu_name=str(gpu_info.get("name") or ""),
        gpu_memory_total_mb=_int(gpu_info.get("memory_total_mb"), 0),
        gpu_memory_used_mb=_int(gpu_info.get("memory_used_mb"), 0),
    )


def _recommend(snapshot: LocalResourceSnapshot) -> dict[str, Any]:
    logical = max(1, snapshot.logical_processors)
    physical = max(1, snapshot.physical_cores)
    available_gb = snapshot.available_memory_mb / 1024 if snapshot.available_memory_mb else 0
    load = max(0.0, min(100.0, snapshot.cpu_percent))

    base_media = max(1, min(6, physical // 4 + 1))
    if logical >= 24 and physical >= 12:
        base_media = max(base_media, 4)
    if load >= 75:
        base_media = max(1, base_media - 2)
    elif load >= 55:
        base_media = max(1, base_media - 1)
    elif load <= 25 and available_gb >= 8 and physical >= 12:
        base_media = min(6, base_media + 1)
    if available_gb and available_gb < 4:
        base_media = min(base_media, 2)

    ocr_cpu = max(1, min(base_media, 5))
    asr_cpu = max(1, min(2 if physical >= 8 else 1, base_media))
    file_io = max(1, min(4, base_media if base_media <= 3 else base_media - 1))
    return {
        "media_cpu": base_media,
        "ocr_cpu_parallel": ocr_cpu,
        "asr_cpu_parallel": asr_cpu,
        "file_io_parallel": file_io,
        "gpu_media": 1,
        "llm_interactive_ratio": 0.7,
        "llm_background_ratio": 0.3,
        "worker_env": {
            "OMP_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "2",
            "NUMEXPR_NUM_THREADS": "2",
        },
        "thermal_risk": _thermal_risk(snapshot, base_media),
        "reason": _recommend_reason(snapshot, base_media, ocr_cpu, asr_cpu),
    }


def _recommend_reason(snapshot: LocalResourceSnapshot, media_cpu: int, ocr_cpu: int, asr_cpu: int) -> str:
    return (
        f"{snapshot.cpu_name}，{snapshot.physical_cores}C/{snapshot.logical_processors}T，"
        f"当前 CPU {snapshot.cpu_percent:.1f}%，可用内存 {snapshot.available_memory_mb}MB；"
        f"建议 media_cpu={media_cpu}，OCR={ocr_cpu}，ASR={asr_cpu}。"
    )


def _thermal_risk(snapshot: LocalResourceSnapshot, media_cpu: int) -> str:
    name = snapshot.cpu_name.lower()
    mobile_hint = any(token in name for token in ("hx", "hs", "h ", "mobile", "laptop"))
    if snapshot.cpu_percent >= 75:
        return "high"
    if mobile_hint and media_cpu >= 5:
        return "medium"
    return "low"


def _windows_cpu_info() -> dict[str, Any]:
    script = (
        "Get-CimInstance Win32_Processor | Select-Object -First 1 "
        "Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress"
    )
    payload = _powershell_json(script)
    return {
        "name": payload.get("Name", ""),
        "physical_cores": payload.get("NumberOfCores", 0),
        "logical_processors": payload.get("NumberOfLogicalProcessors", 0),
    }


def _windows_memory_info() -> dict[str, Any]:
    computer = _powershell_json(
        "Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 TotalPhysicalMemory | ConvertTo-Json -Compress"
    )
    os_info = _powershell_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 FreePhysicalMemory | ConvertTo-Json -Compress"
    )
    return {
        "total_mb": int(_int(computer.get("TotalPhysicalMemory"), 0) / 1024 / 1024),
        "available_mb": int(_int(os_info.get("FreePhysicalMemory"), 0) / 1024),
    }


def _powershell_json(script: str) -> dict[str, Any]:
    import json

    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cpu_percent_sample() -> float:
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 2).CounterSamples[-1].CookedValue",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return round(float(completed.stdout.strip()), 2)
        except Exception:
            return 0.0
    try:
        first = _read_proc_stat()
        time.sleep(0.5)
        second = _read_proc_stat()
        idle = second[3] - first[3]
        total = sum(second) - sum(first)
        return round(max(0.0, min(100.0, 100.0 * (1.0 - idle / max(1, total)))), 2)
    except Exception:
        return 0.0


def _read_proc_stat() -> list[int]:
    parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    return [int(item) for item in parts]


def _nvidia_smi_info() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return {}
    if completed.returncode != 0:
        return {}
    line = next((item.strip() for item in completed.stdout.splitlines() if item.strip()), "")
    if not line:
        return {}
    parts = [item.strip() for item in line.split(",")]
    return {
        "name": parts[0] if parts else "",
        "memory_total_mb": parts[1] if len(parts) > 1 else 0,
        "memory_used_mb": parts[2] if len(parts) > 2 else 0,
    }


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
