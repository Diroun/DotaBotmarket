"""
GitHub Releases updater for the folder-based Windows distribution.

Release convention:
- Create a GitHub Release in Diroun/DotaBotmarket.
- Tag it with a version, e.g. v1.0.1.
- Attach a ZIP asset containing main.py and the application .py files
  at the archive root (or in one enclosing folder).
"""
import os
import re
import sys
import time
import zipfile
import tempfile
import subprocess
from pathlib import Path

import requests

GITHUB_REPO = "Diroun/DotaBotmarket"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CURRENT_VERSION = "1.0.0"
REQUEST_TIMEOUT = (5, 30)


def _version_tuple(value):
    parts = re.findall(r"\d+", str(value))
    return tuple(int(part) for part in parts) if parts else (0,)


def version_is_newer(latest, current=CURRENT_VERSION):
    return _version_tuple(latest) > _version_tuple(current)


def check_latest_release():
    response = requests.get(
        RELEASE_API,
        headers={"Accept": "application/vnd.github+json"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    release = response.json()
    if release.get("draft") or release.get("prerelease"):
        return None
    return release


def download_and_launch_update(release):
    """Download release ZIP and start a detached Windows updater."""
    if os.name != "nt":
        return False, "Автоустановка сейчас поддерживается в Windows."

    assets = release.get("assets") or []
    zip_asset = next(
        (
            asset for asset in assets
            if str(asset.get("name", "")).lower().endswith(".zip")
            and asset.get("browser_download_url")
        ),
        None,
    )
    if not zip_asset:
        return False, (
            "В GitHub Release нет ZIP-архива. "
            "При публикации релиза прикрепи ZIP с файлами программы."
        )

    app_dir = Path(__file__).resolve().parent
    update_dir = Path(tempfile.gettempdir()) / "DotaBotmarket_update"
    update_dir.mkdir(parents=True, exist_ok=True)
    archive_path = update_dir / "release.zip"

    try:
        with requests.get(
            zip_asset["browser_download_url"],
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()
            with archive_path.open("wb") as target:
                for chunk in response.iter_content(1024 * 128):
                    if chunk:
                        target.write(chunk)

        # Проверяем, что архив читается до запуска установщика.
        with zipfile.ZipFile(archive_path, "r") as archive:
            bad_file = archive.testzip()
            if bad_file:
                raise ValueError(f"Повреждён файл в архиве: {bad_file}")

        pid = os.getpid()
        app_path = str(app_dir)
        zip_path = str(archive_path)
        log_path = str(update_dir / "update.log")

        # PowerShell ждёт завершения текущего процесса, делает резервную
        # копию кода и заменяет файлы, не затрагивая пользовательскую data/.
        ps = r"""
$ErrorActionPreference = 'Stop'
$pidToWait = __PID__
$app = '__APP__'
$zip = '__ZIP__'
$log = '__LOG__'
$python = '__PYTHON__'
$work = Join-Path $env:TEMP 'DotaBotmarket_update_unpack'
$backup = Join-Path $env:TEMP ('DotaBotmarket_backup_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
try {
    while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 500 }
    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $work -Force
    $src = $work
    if (!(Test-Path (Join-Path $src 'main.py'))) {
        $dirs = @(Get-ChildItem -LiteralPath $work -Directory)
        if ($dirs.Count -eq 1 -and (Test-Path (Join-Path $dirs[0].FullName 'main.py'))) {
            $src = $dirs[0].FullName
        }
    }
    if (!(Test-Path (Join-Path $src 'main.py'))) { throw 'В ZIP не найден main.py в корне проекта.' }
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Get-ChildItem -LiteralPath $app -Force | Where-Object {
        $_.Name -notin @('data', '.git', '.venv', 'venv', '__pycache__')
    } | Copy-Item -Destination $backup -Recurse -Force
    Get-ChildItem -LiteralPath $src -Force | Where-Object {
        $_.Name -notin @('data', '.git', '.venv', 'venv', '__pycache__')
    } | Copy-Item -Destination $app -Recurse -Force
    Add-Content -LiteralPath $log -Value 'Update installed successfully.'
    Start-Process -FilePath $python -ArgumentList 'main.py' -WorkingDirectory $app 
} catch {
    Add-Content -LiteralPath $log -Value $_.Exception.Message
    if (Test-Path $backup) {
        Get-ChildItem -LiteralPath $backup -Force | Copy-Item -Destination $app -Recurse -Force
    }
}
"""
        def ps_quote(value):
            return str(value).replace("'", "''")

        ps = (ps.replace("__PID__", str(pid))
                .replace("__APP__", ps_quote(app_path))
                .replace("__ZIP__", ps_quote(zip_path))
                .replace("__LOG__", ps_quote(log_path))
                .replace("__PYTHON__", ps_quote(sys.executable)))
        ps_path = update_dir / "install_update.ps1"
        ps_path.write_text(ps, encoding="utf-8-sig")

        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(ps_path),
        ]
        subprocess.Popen(
            command,
            cwd=str(app_dir),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
        return True, "Обновление подготовлено."
    except Exception as exc:
        return False, f"Не удалось подготовить обновление: {exc}"
