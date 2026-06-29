from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LibreOfficeHealth:
    available: bool
    executable: str = ""
    version: str = ""


class LibreOfficeRuntime:
    def __init__(self, executable: str | None = None):
        self.executable = executable or _find_soffice()

    def health(self) -> LibreOfficeHealth:
        if not self.executable:
            return LibreOfficeHealth(available=False)
        executable = Path(self.executable)
        if not executable.exists():
            return LibreOfficeHealth(available=False, executable=self.executable)
        version_hint = _read_version_ini(executable)
        if _is_project_vendor_executable(executable) and version_hint:
            return LibreOfficeHealth(available=True, executable=self.executable, version=version_hint)
        try:
            completed = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                cwd=str(executable.parent),
            )
        except (OSError, subprocess.TimeoutExpired):
            return LibreOfficeHealth(available=False, executable=self.executable)
        version = (completed.stdout or completed.stderr).strip() or version_hint
        return LibreOfficeHealth(available=completed.returncode == 0, executable=self.executable, version=version)

    def convert_to_pdf(self, input_path: str | Path, output_dir: str | Path) -> Path:
        health = self.health()
        if not health.available:
            raise RuntimeError("LibreOffice is not available")
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        profile_dir = output / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        command = _convert_command(health.executable, input_path, output, profile_dir)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(Path(health.executable).parent),
            stdin=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        return output / (Path(input_path).stem + ".pdf")


def _find_soffice() -> str:
    project_candidate = Path(__file__).resolve().parents[4] / "vendor" / "libreoffice" / "program" / "soffice.exe"
    if project_candidate.exists():
        return str(project_candidate)
    for name in ["soffice", "libreoffice"]:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _convert_command(executable: str, input_path: str | Path, output_dir: Path, profile_dir: Path) -> list[str]:
    return [
        executable,
        "--headless",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--norestore",
        f"-env:UserInstallation={_file_url(profile_dir)}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(Path(input_path)),
    ]


def _file_url(path: str | Path) -> str:
    resolved = Path(path).resolve()
    return resolved.as_uri()


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _read_version_ini(executable: Path) -> str:
    version_ini = executable.parent / "version.ini"
    if not version_ini.exists():
        return ""
    values: dict[str, str] = {}
    for line in version_ini.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    vendor = values.get("Vendor", "LibreOffice")
    buildid = values.get("buildid", "")
    return f"{vendor} build {buildid}".strip()


def _is_project_vendor_executable(executable: Path) -> bool:
    parts = {part.lower() for part in executable.parts}
    return "vendor" in parts and "libreoffice" in parts
