#!/usr/bin/env python3
"""Run a clean-HOME, clean-install-capable real browser product acceptance."""

from __future__ import annotations

import argparse
import base64
import json
import os
import selectors
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

REPO = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", default=shutil.which("copperwright"))
    parser.add_argument("--geckodriver", default=shutil.which("geckodriver"))
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def initialize_clean_home(home: Path) -> None:
    target = home / ".config" / "kicad" / "10.0"
    target.mkdir(parents=True, mode=0o700)
    template = Path("/usr/share/kicad/template")
    for name in ("sym-lib-table", "fp-lib-table"):
        source = template / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"KiCad library-table template unavailable: {source}")
        shutil.copy2(source, target / name)


def start_server(
    executable: str,
    workspace: Path,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [
            executable,
            "app",
            "--provider",
            "builtin",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--no-open",
        ],
        cwd=REPO,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    line = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(
                f"CopperWright app exited early ({process.returncode}): {stderr[-2000:]}"
            )
        if selector.select(timeout=0.25):
            line = process.stdout.readline().strip()
            if line.startswith("CopperWright app: http://127.0.0.1:"):
                return process, line.removeprefix("CopperWright app: ")
    process.terminate()
    raise RuntimeError(
        f"timed out waiting for CopperWright app URL; last output: {line}"
    )


def stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class WebDriver:
    """Small W3C WebDriver client; no Selenium package is required."""

    def __init__(self, executable: str) -> None:
        self.port = available_port()
        help_result = subprocess.run(
            [executable, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        driver_args = [
            executable,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log",
            "fatal",
        ]
        if "--allow-system-access" in help_result.stdout:
            # Required by geckodriver 0.37.1+ for browser-UI automation and safe
            # to request on 0.37.0. The driver remains bound to loopback.
            driver_args.append("--allow-system-access")
        self.process = subprocess.Popen(
            driver_args,
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.root = f"http://127.0.0.1:{self.port}"
        self.session_id = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise RuntimeError(f"geckodriver exited early: {stderr[-2000:]}")
            try:
                if self._request("GET", "/status").get("ready"):
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(0.1)
        else:
            self.close()
            raise RuntimeError("timed out waiting for geckodriver")
        value = self._request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "firefox",
                        "acceptInsecureCerts": False,
                        "moz:firefoxOptions": {
                            "args": ["-headless"],
                            "prefs": {
                                "browser.shell.checkDefaultBrowser": False,
                                "datareporting.policy.dataSubmissionEnabled": False,
                                "toolkit.telemetry.enabled": False,
                            },
                        },
                    }
                }
            },
            timeout=60,
        )
        self.session_id = str(value["sessionId"])
        self.command(
            "POST",
            "/timeouts",
            {"implicit": 0, "pageLoad": 30000, "script": 30000},
        )
        self.command(
            "POST",
            "/window/rect",
            {"x": 0, "y": 0, "width": 1440, "height": 1000},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> Any:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.root + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                envelope = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(
                f"WebDriver {method} {path} failed: {body[-4000:]}"
            ) from exc
        value = envelope.get("value")
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(
                f"WebDriver {method} {path}: {value['error']}: {value.get('message', '')}"
            )
        return value

    def command(
        self,
        method: str,
        suffix: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> Any:
        return self._request(
            method,
            f"/session/{self.session_id}{suffix}",
            payload,
            timeout=timeout,
        )

    def navigate(self, url: str) -> None:
        self.command("POST", "/url", {"url": url}, timeout=60)

    def execute(self, script: str) -> Any:
        return self.command(
            "POST",
            "/execute/sync",
            {"script": script, "args": []},
        )

    def wait(self, script: str, description: str, timeout: float = 420) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self.execute(script):
                    return
            except RuntimeError as exc:
                last_error = str(exc)
            time.sleep(0.25)
        try:
            page = self.execute(
                "return {url: location.href, title: document.title, "
                "ready: document.readyState, eyebrow: "
                "document.querySelector('#project-eyebrow')?.textContent, "
                "job: document.querySelector('#job-detail')?.textContent, "
                "toast: document.querySelector('#toast')?.textContent};"
            )
        except RuntimeError as exc:
            page = {"diagnostic_error": str(exc)}
        raise RuntimeError(
            f"timed out waiting for {description}; last_error={last_error}; "
            f"page={json.dumps(page, ensure_ascii=False)}"
        )

    def screenshot(self, path: Path) -> None:
        encoded = self.command("GET", "/screenshot")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded, validate=True))

    def close(self) -> None:
        if self.session_id:
            try:
                self._request("DELETE", f"/session/{self.session_id}", timeout=15)
            except (OSError, RuntimeError):
                pass
            self.session_id = ""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object from {url}")
    return value


def read_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def wait_value(
    reader: Callable[[], dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    description: str,
    *,
    timeout: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = reader()
        if predicate(last):
            return last
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {description}: {last}")


def project_view(base_url: str, project_id: str) -> dict[str, Any]:
    return read_json(f"{base_url}/api/projects/{project_id}")


def no_active_jobs(view: dict[str, Any]) -> bool:
    return not any(
        job["status"] in {"queued", "running", "cancel_requested"}
        for job in view.get("jobs", [])
    )


def primary_browser_flow(
    geckodriver: str,
    base_url: str,
    output: Path,
) -> dict[str, Any]:
    with WebDriver(geckodriver) as browser:
        browser.navigate(base_url)
        browser.wait(
            "return document.readyState === 'complete' && "
            "document.querySelector('#diagnostics')?.textContent.includes('Workspace');",
            "CopperWright bootstrap",
            timeout=30,
        )
        browser.execute("document.querySelector('#provider-setup').click();")
        browser.wait(
            "return document.querySelector('#setup-dialog').open;",
            "provider setup dialog",
        )
        setup_safe = browser.execute(
            "const dialog = document.querySelector('#setup-dialog'); "
            "return dialog.textContent.includes('Active provider: builtin') && "
            "dialog.textContent.includes('OPENAI_API_KEY=<secret>') && "
            "dialog.querySelectorAll('input').length === 0;"
        )
        if not setup_safe:
            raise RuntimeError(
                "provider setup did not preserve the no-browser-secret contract"
            )
        browser.execute("document.querySelector('#close-setup').click();")

        browser.execute("document.querySelector('#new-project').click();")
        browser.wait(
            "return document.querySelector('#new-project-dialog').open;",
            "new-project dialog",
        )
        browser.execute(
            "document.querySelector('#new-project-name').value = 'Browser greenhouse'; "
            "document.querySelector('#new-project-request').value = "
            "'Create a BME280 SPI environmental sensor and controller board powered by regulated 3.3 V'; "
            "document.querySelector('#new-project-form').requestSubmit();"
        )
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('needs clarification');",
            "focused layer clarification",
        )
        question = str(
            browser.execute(
                "return document.querySelector('#messages article:last-of-type "
                ".message-body p')?.textContent || '';"
            )
        )
        if "2 or 4 copper layers" not in question:
            raise RuntimeError(f"unexpected clarification: {question}")
        browser.execute(
            "[...document.querySelectorAll('#messages .scope-chips button')]"
            ".find((item) => item.textContent === '2 layers').click();"
        )
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('awaiting confirmation');",
            "reviewable proposal",
        )
        review_ready = browser.execute(
            "const brief = document.querySelector('#tab-brief').textContent; "
            "return brief.includes('BME280') && brief.includes('BOM') && "
            "brief.includes('Constraints') && "
            "brief.includes('human engineering review') && "
            "document.querySelector('#open-kicad').disabled;"
        )
        if not review_ready:
            raise RuntimeError(
                "pre-generation brief/BOM/constraint review is incomplete"
            )
        browser.screenshot(output / "copperwright-app-brief.png")

        project_id = str(
            browser.execute(
                "return document.querySelector('.project-button.active').dataset.projectId;"
            )
        )
        browser.execute("document.querySelector('#confirm').click();")
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('validated');",
            "real KiCad generation and validation",
        )
        generated = wait_value(
            lambda: project_view(base_url, project_id),
            lambda view: bool(
                view.get("design")
                and view["artifacts"]["previews"]
                and view["artifacts"]["validation"]
                and no_active_jobs(view)
            ),
            "generated project view",
        )
        validation = generated["artifacts"]["validation"]
        if (
            not validation["candidate_ready"]
            or validation["production_ready"]
            or validation["production_claimed"]
        ):
            raise RuntimeError("browser generation made an invalid readiness claim")
        first_hash = generated["design"]["content_hash"]
        if browser.execute("return document.querySelector('#open-kicad').disabled;"):
            raise RuntimeError("Open in KiCad action was not enabled")

        browser.execute(
            "[...document.querySelectorAll('.tab')]"
            ".find((item) => item.dataset.tab === 'artifacts').click();"
        )
        browser.wait(
            "return [...document.querySelectorAll('#tab-artifacts img.preview')]"
            ".length === 2 && "
            "[...document.querySelectorAll('#tab-artifacts img.preview')]"
            ".every((image) => image.complete && image.naturalWidth > 0);",
            "real KiCad browser previews",
            timeout=60,
        )
        browser.screenshot(output / "copperwright-app-visuals.png")

        browser.execute(
            "const input = document.querySelector('#message-input'); "
            "input.value = 'Change this board to 4 layers'; "
            "document.querySelector('#composer').requestSubmit();"
        )
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('change ready');",
            "validated semantic diff",
        )
        staged = wait_value(
            lambda: project_view(base_url, project_id),
            no_active_jobs,
            "completed semantic preview job",
        )
        change = staged["active_change"]
        layer_diff = change["diff"]["board_fields"]["layers"]
        if (
            not change["validation"]["candidate_ready"]
            or layer_diff != {"before": 2, "after": 4}
            or staged["design"]["content_hash"] != first_hash
        ):
            raise RuntimeError(
                "semantic preview changed authoritative files or omitted the layer diff"
            )
        browser.execute("document.querySelector('#confirm').click();")
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('validated');",
            "atomic semantic apply",
        )
        applied = wait_value(
            lambda: project_view(base_url, project_id),
            lambda view: (
                view.get("design", {}).get("content_hash") != first_hash
                and no_active_jobs(view)
            ),
            "applied semantic hash",
        )
        applied_hash = applied["design"]["content_hash"]
        browser.wait(
            "const panel = document.querySelector('#job-panel'); "
            "const title = document.querySelector('#job-title')?.textContent || ''; "
            "return panel.classList.contains('hidden') || "
            "title.includes('completed');",
            "stable post-transaction UI",
        )

        browser.execute(
            "[...document.querySelectorAll('.tab')]"
            ".find((item) => item.dataset.tab === 'validation').click();"
        )
        browser.wait(
            "return document.querySelectorAll('#tab-validation .level-row').length === 8;",
            "L0-L7 results",
        )
        external_gates = browser.execute(
            "const text = document.querySelector('#tab-validation').textContent; "
            "return text.includes('l6.engineering_review') && "
            "text.includes('l7.physical_build_test') && "
            "text.includes('Not production-signed');"
        )
        if not external_gates:
            raise RuntimeError("honest human/physical gates are not visible")
        browser.screenshot(output / "copperwright-app-validation.png")
        browser.wait(
            "return ![...document.querySelectorAll('#tab-validation button')]"
            ".find((item) => item.textContent === 'Undo last change').disabled;",
            "enabled undo action",
        )
        browser.execute(
            "[...document.querySelectorAll('#tab-validation button')]"
            ".find((item) => item.textContent === 'Undo last change').click();"
        )
        wait_value(
            lambda: project_view(base_url, project_id),
            lambda view: (
                view.get("design", {}).get("content_hash") == first_hash
                and no_active_jobs(view)
            ),
            "exact semantic undo",
        )

        browser.wait(
            "return !document.querySelector('#release').disabled;",
            "enabled candidate export after undo",
        )
        browser.execute("document.querySelector('#release').click();")
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('released');",
            "manufacturing-candidate release",
        )
        released = wait_value(
            lambda: project_view(base_url, project_id),
            lambda view: bool(view["artifacts"]["release"]) and no_active_jobs(view),
            "offline release verification",
        )
        release = released["artifacts"]["release"]
        if not release["offline_verification"]["verified"]:
            raise RuntimeError("offline manufacturing-candidate verification failed")
        report = read_json(
            f"{base_url}/api/projects/{project_id}/artifact/validation_report"
        )
        archive = read_bytes(
            f"{base_url}/api/projects/{project_id}/artifact/release_archive"
        )
        if len(report.get("levels", [])) != 8 or len(archive) < 1000:
            raise RuntimeError("browser validation/release artifacts are incomplete")

        browser.execute("document.querySelector('#new-project').click();")
        browser.wait(
            "return document.querySelector('#new-project-dialog').open;",
            "unsupported-project dialog",
        )
        browser.execute(
            "document.querySelector('#new-project-name').value = 'Unsupported USB'; "
            "document.querySelector('#new-project-request').value = "
            "'Create a USB-C mains-powered medical board'; "
            "document.querySelector('#new-project-form').requestSubmit();"
        )
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('unsupported');",
            "explicit unsupported scope",
        )
        unsupported_visible = browser.execute(
            "const brief = document.querySelector('#tab-brief'); "
            "return !brief.classList.contains('hidden') && "
            "document.querySelector('[data-tab=brief]').getAttribute('aria-selected') === 'true' && "
            "brief.textContent.includes('Rejected') && "
            "brief.textContent.includes('outside the supported');"
        )
        if not unsupported_visible:
            raise RuntimeError("unsupported scope was not clearly visible")
        browser.screenshot(output / "copperwright-app-unsupported.png")
        browser.execute(
            "[...document.querySelectorAll('.project-button')]"
            f".find((item) => item.dataset.projectId === {json.dumps(project_id)}).click();"
        )
        browser.wait(
            "return document.querySelector('#project-eyebrow').textContent"
            ".startsWith('released');",
            "return to released project",
        )
        return {
            "project_id": project_id,
            "clarification": question,
            "original_hash": first_hash,
            "applied_hash": applied_hash,
            "undo_restored_original_hash": True,
            "candidate_ready": True,
            "production_ready": False,
            "offline_release_verified": True,
            "validation_levels": len(report["levels"]),
            "release_archive_bytes": len(archive),
            "unsupported_scope_visible": True,
        }


def reopen_browser_flow(
    geckodriver: str,
    base_url: str,
    output: Path,
    project_id: str,
) -> dict[str, Any]:
    with WebDriver(geckodriver) as browser:
        browser.navigate(base_url)
        expected = json.dumps(project_id)
        browser.wait(
            "return [...document.querySelectorAll('.project-button')]"
            f".some((item) => item.dataset.projectId === {expected});",
            "persisted project in restart list",
            timeout=30,
        )
        browser.execute(
            "[...document.querySelectorAll('.project-button')]"
            f".find((item) => item.dataset.projectId === {expected}).click();"
        )
        browser.wait(
            "return document.querySelector('.project-button.active')?.dataset.projectId === "
            f"{expected};",
            "persisted project selection",
            timeout=30,
        )
        view = project_view(base_url, project_id)
        result = {
            "project_id": view["project"]["id"],
            "selected_from_project_list": True,
            "status": view["project"]["status"],
            "messages": len(view["conversation"]["messages"]),
            "design": bool(view["design"]),
            "release_verified": bool(
                view["artifacts"]["release"]["offline_verification"]["verified"]
            ),
            "active_job": any(
                job["status"] in {"queued", "running", "cancel_requested"}
                for job in view["jobs"]
            ),
        }
        if (
            result["status"] != "released"
            or result["messages"] < 7
            or not result["design"]
            or not result["release_verified"]
            or result["active_job"]
        ):
            raise RuntimeError(f"persisted project did not reopen cleanly: {result}")
        browser.screenshot(output / "copperwright-app-reopened.png")
        return result


def main() -> int:
    arguments = parse_args()
    if not arguments.executable or not Path(arguments.executable).is_file():
        raise SystemExit("copperwright executable is required")
    if not arguments.geckodriver or not Path(arguments.geckodriver).is_file():
        raise SystemExit("geckodriver is required for the real browser flow")
    if not shutil.which("kicad-cli"):
        raise SystemExit("real KiCad is required")

    output = (
        arguments.output
        or Path(tempfile.mkdtemp(prefix="copperwright-browser-evidence-"))
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="copperwright-browser-e2e-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        initialize_clean_home(home)
        workspace = root / "workspace"
        environment = dict(os.environ)
        environment["HOME"] = str(home)
        environment["XDG_CONFIG_HOME"] = str(home / ".config")

        first_server, first_url = start_server(
            arguments.executable, workspace, environment
        )
        try:
            primary = primary_browser_flow(arguments.geckodriver, first_url, output)
        finally:
            stop_server(first_server)

        second_server, second_url = start_server(
            arguments.executable, workspace, environment
        )
        try:
            reopened = reopen_browser_flow(
                arguments.geckodriver,
                second_url,
                output,
                str(primary["project_id"]),
            )
        finally:
            stop_server(second_server)

    report = {
        "schema": "copperwright-browser-e2e",
        "version": 1,
        "browser": "firefox-webdriver",
        "clean_home": True,
        "loopback_only": True,
        "provider": "builtin",
        "primary_flow": primary,
        "reopen_flow": reopened,
        "screenshots": sorted(path.name for path in output.glob("*.png")),
    }
    report_path = output / "browser-e2e.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"report": str(report_path), **report},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
