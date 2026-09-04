from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.interfaces.gui import _open_project_in_kicad, create_gui_app


class _Service:
    def __init__(self) -> None:
        self.project_id = "demo-board"
        self.activity = [
            {
                "sequence": 1,
                "kind": "generation.complete",
                "level": "info",
                "message": "Committed geometry is ready",
                "created_at": "2026-08-30T00:00:00Z",
            }
        ]

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "id": self.project_id,
                "name": "Demo board",
                "status": "generated",
                "updated_at": "2026-08-30T00:00:00Z",
                "design_revision": 3,
                "provider": "hidden-provider-field",
                "root": "/must/not/leak",
            }
        ]

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != self.project_id:
            raise PCBDraftError("project not found")
        return {"project": {"id": project_id}}

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        self.open_project(project_id)
        return [value for value in self.activity if value["sequence"] > after]


class _LiveView:
    def __init__(self) -> None:
        self.scene = {
            "schema": "pcbdraft-live-board-scene",
            "version": 1,
            "project_id": "demo-board",
            "state_revision": 7,
            "design_revision": 3,
            "geometry_revision": 2,
            "content_hash": "a" * 64,
            "board": {"width_mm": 80.0, "height_mm": 50.0, "layers": ["F.Cu", "B.Cu"]},
            "outline": [],
            "footprints": [],
            "routes": [],
            "vias": [],
            "unrouted_nets": [],
            "status": {"project": "generated", "validation": None},
        }

    def snapshot(self, project_id: str, *, timeout: float = 0.0) -> dict[str, Any]:
        if project_id != "demo-board" or timeout != 0.0:
            raise AssertionError("unexpected scene request")
        return dict(self.scene)


class _Previews:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.board = root / "board.svg"
        self.board.write_text(
            "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
        )
        self.image = root / "board-top.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\npreview")

    def artifact(
        self,
        project_id: str,
        kind: str,
        *,
        generate: bool = False,
        timeout: float = 90.0,
    ) -> Path | None:
        if project_id != "demo-board" or timeout != 90.0:
            raise AssertionError("unexpected preview request")
        if kind == "board_svg":
            return self.board
        if kind == "board_3d":
            return self.image if generate else None
        raise AssertionError("unknown kind")


class _IPC:
    def poll(self, scene: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "offline",
            "available": False,
            "message": "KiCad IPC is not connected",
            "source_content_hash": scene["content_hash"],
        }


class _Sessions:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.shutdown_calls = 0
        self._events: list[dict[str, Any]] = []

    def start(self, project_id: str, text: object) -> dict[str, Any]:
        if not isinstance(text, str):
            raise PCBDraftError("message text must be a string")
        self.started.append((project_id, text))
        self._events.append(
            {
                "sequence": len(self._events) + 1,
                "kind": "turn.started",
                "message": "Agent turn started",
                "level": "info",
                "created_at": "2026-08-30T00:00:01Z",
                "prompt": "must not leak",
                "arguments": {"secret": True},
            }
        )
        return {"project_id": project_id, "status": "running"}

    def stop(self, project_id: str) -> dict[str, Any]:
        self.stopped.append(project_id)
        return {"project_id": project_id, "status": "stopping"}

    def session(self, project_id: str) -> dict[str, Any]:
        return {
            "schema": "pcbdraft-gui-session",
            "version": 1,
            "project_id": project_id,
            "status": "idle",
            "messages": [],
        }

    def events(self, project_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        return [item for item in self._events if item["sequence"] > after]

    def shutdown(self) -> list[dict[str, Any]]:
        self.shutdown_calls += 1
        return []


class _KiCadOpener:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, project_id: str) -> dict[str, Any]:
        self.opened.append(project_id)
        return {"opened": True, "project_id": project_id}


class _Artifacts:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bom = root / "bom.csv"
        self.bom.write_text("Reference,Value\nU1,Demo\n", encoding="utf-8")

    def validation(self, project_id: str) -> dict[str, Any]:
        if project_id != "demo-board":
            raise AssertionError("unexpected artifact project")
        return {
            "schema": "pcbdraft-gui-validation",
            "version": 1,
            "state": "pass",
            "design_revision": 3,
            "checks": [],
        }

    def manifest(self, project_id: str) -> dict[str, Any]:
        if project_id != "demo-board":
            raise AssertionError("unexpected artifact project")
        return {
            "schema": "pcbdraft-gui-artifacts",
            "version": 1,
            "artifacts": [
                {
                    "key": "bom.csv",
                    "label": "BOM",
                    "state": "ready",
                    "file_count": 1,
                    "bytes": self.bom.stat().st_size,
                    "created_at": "2026-08-30T00:00:00Z",
                }
            ],
        }

    def download(self, project_id: str, key: str) -> SimpleNamespace:
        if project_id != "demo-board" or key != "bom.csv":
            raise AssertionError("unexpected fixed artifact request")
        return SimpleNamespace(
            path=self.bom,
            media_type="text/csv; charset=utf-8",
            filename="bom.csv",
        )


class _BoardService:
    def __init__(self, project_root: Path, board_path: Path) -> None:
        self.project = project_root
        self.board = board_path

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != "board-one":
            raise PCBDraftError("project not found")
        return {"design": {"files": {"board": str(self.board)}}}

    def project_root(self, project_id: str) -> Path:
        self.open_project(project_id)
        return self.project


class KiCadDesktopLaunchTests(unittest.TestCase):
    def test_launcher_uses_only_the_selected_project_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "repository" / "projects" / "board-one"
            design = project / "design"
            design.mkdir(parents=True)
            board = design / "board-one.kicad_pcb"
            board.write_text("(kicad_pcb)", encoding="utf-8")
            outside = root / "other.kicad_pcb"
            outside.write_text("(kicad_pcb)", encoding="utf-8")
            service = _BoardService(project, board)

            with (
                mock.patch(
                    "pcbdraft.interfaces.gui.find_kicad_app",
                    return_value="/opt/kicad/bin/kicad",
                ),
                mock.patch("pcbdraft.interfaces.gui.subprocess.Popen") as popen,
            ):
                result = _open_project_in_kicad(service, "board-one")  # type: ignore[arg-type]
                service.board = outside
                with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                    _open_project_in_kicad(service, "board-one")  # type: ignore[arg-type]

            self.assertEqual(result, {"opened": True, "project_id": "board-one"})
            popen.assert_called_once()
            self.assertEqual(
                popen.call_args.args[0], ["/opt/kicad/bin/kicad", str(board)]
            )
            self.assertEqual(popen.call_args.kwargs["cwd"], design)


class GUIApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = _Service()
        self.sessions = _Sessions()
        self.kicad_opener = _KiCadOpener()
        self.artifacts = _Artifacts(self.root)
        self.app = create_gui_app(
            self.service,  # type: ignore[arg-type]
            cache_root=self.root / "cache",
            initial_project="demo-board",
            bind_host="127.0.0.1",
            allowed_hosts={"testserver"},
            live_view=_LiveView(),
            previews=_Previews(self.root),
            ipc=_IPC(),
            sessions=self.sessions,
            kicad_opener=self.kicad_opener,
            artifacts=self.artifacts,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def _bootstrap(self, *, prefix: str = "") -> dict[str, Any]:
        response = await self.client.get(f"{prefix}/api/bootstrap")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def _mutation_headers(self, *, prefix: str = "") -> dict[str, str]:
        bootstrap = await self._bootstrap(prefix=prefix)
        return {
            "Origin": "http://testserver",
            "X-PCBDraft-CSRF": bootstrap["csrf_token"],
            "Content-Type": "application/json",
        }

    async def test_root_and_proxy_prefix_serve_the_same_relative_application(
        self,
    ) -> None:
        asset_names = (
            "app.css",
            "tokens.css",
            "workbench.css",
            "app.js",
            "api.js",
            "store.js",
            "i18n.js",
            "commands.js",
            "board.js",
            "projects.js",
            "inspector.js",
            "conversation.js",
        )
        for prefix in ("", "/pcbdraft"):
            with self.subTest(prefix=prefix):
                document = await self.client.get(f"{prefix}/")
                script = await self.client.get(f"{prefix}/assets/app.js")
                self.assertEqual(document.status_code, 200)
                self.assertEqual(script.status_code, 200)
                for asset_name in asset_names:
                    with self.subTest(prefix=prefix, asset=asset_name):
                        asset = await self.client.get(f"{prefix}/assets/{asset_name}")
                        self.assertEqual(asset.status_code, 200)
                self.assertIn('href="./assets/app.css"', document.text)
                self.assertIn('src="./assets/app.js"', document.text)
                self.assertNotIn('href="/assets/', document.text)
                self.assertNotIn('src="/assets/', document.text)
                self.assertIn("document.baseURI", script.text)

    async def test_validation_and_manufacturing_routes_are_fixed_and_public_only(
        self,
    ) -> None:
        validation = await self.client.get("/api/projects/demo-board/validation")
        manifest = await self.client.get("/api/projects/demo-board/artifacts")
        download = await self.client.get("/api/projects/demo-board/artifacts/bom.csv")
        traversal = await self.client.get(
            "/api/projects/demo-board/artifacts/../../project.json"
        )

        self.assertEqual(validation.status_code, 200, validation.text)
        self.assertEqual(validation.json()["state"], "pass")
        self.assertEqual(manifest.status_code, 200, manifest.text)
        self.assertEqual(manifest.json()["artifacts"][0]["key"], "bom.csv")
        self.assertNotIn(str(self.root), manifest.text)
        self.assertEqual(download.status_code, 200)
        self.assertIn("text/csv", download.headers["content-type"])
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(traversal.status_code, 404)

    async def test_healthz_is_bounded_and_available_at_both_mount_paths(self) -> None:
        for prefix in ("", "/pcbdraft"):
            with self.subTest(prefix=prefix):
                response = await self.client.get(f"{prefix}/healthz")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {
                        "schema": "pcbdraft-gui-health",
                        "version": 1,
                        "status": "ok",
                    },
                )
                self.assertLess(len(response.content), 256)

    async def test_project_and_snapshot_endpoints_expose_only_bounded_public_fields(
        self,
    ) -> None:
        projects = await self.client.get("/api/projects")
        snapshot = await self.client.get("/api/projects/demo-board/snapshot")

        self.assertEqual(projects.status_code, 200)
        project = projects.json()["projects"][0]
        self.assertEqual(
            set(project),
            {"id", "name", "status", "updated_at", "design_revision"},
        )
        self.assertNotIn("/must/not/leak", projects.text)
        self.assertEqual(snapshot.status_code, 200)
        self.assertFalse(snapshot.json()["busy"])
        self.assertEqual(snapshot.json()["scene"]["geometry_revision"], 2)
        self.assertEqual(snapshot.json()["ipc"]["status"], "offline")

    async def test_all_requests_reject_an_unsafe_host(self) -> None:
        response = await self.client.get(
            "/api/projects", headers={"Host": "evil.example"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["message"], "invalid Host header")

    async def test_mutations_require_same_origin_csrf_json_and_bounded_body(
        self,
    ) -> None:
        csrf = (await self._bootstrap())["csrf_token"]
        target = "/api/projects/demo-board/messages"
        wrong_origin = await self.client.post(
            target,
            json={"text": "route the board"},
            headers={"Origin": "http://evil.example", "X-PCBDraft-CSRF": csrf},
        )
        wrong_csrf = await self.client.post(
            target,
            json={"text": "route the board"},
            headers={"Origin": "http://testserver", "X-PCBDraft-CSRF": "wrong"},
        )
        wrong_type = await self.client.post(
            target,
            content=b"{}",
            headers={
                "Origin": "http://testserver",
                "X-PCBDraft-CSRF": csrf,
                "Content-Type": "text/plain",
            },
        )
        oversized = await self.client.post(
            target,
            content=b"{}",
            headers={
                "Origin": "http://testserver",
                "X-PCBDraft-CSRF": csrf,
                "Content-Type": "application/json",
                "Content-Length": str(64 * 1024 + 1),
            },
        )
        mismatched_length = await self.client.post(
            target,
            content=b"{}",
            headers={
                "Origin": "http://testserver",
                "X-PCBDraft-CSRF": csrf,
                "Content-Type": "application/json",
                "Content-Length": "3",
            },
        )

        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(wrong_csrf.status_code, 403)
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(mismatched_length.status_code, 400)
        self.assertEqual(self.sessions.started, [])

    async def test_message_and_stop_use_project_bound_session_manager(self) -> None:
        headers = await self._mutation_headers()
        message = await self.client.post(
            "/api/projects/demo-board/messages",
            json={"text": "Place U1 near J1"},
            headers=headers,
        )
        stop = await self.client.post(
            "/api/projects/demo-board/stop", json={}, headers=headers
        )

        self.assertEqual(message.status_code, 202, message.text)
        self.assertEqual(stop.status_code, 202, stop.text)
        self.assertEqual(self.sessions.started, [("demo-board", "Place U1 near J1")])
        self.assertEqual(self.sessions.stopped, ["demo-board"])

    async def test_open_in_kicad_accepts_no_client_path_and_uses_selected_project(
        self,
    ) -> None:
        opened = await self.client.post(
            "/pcbdraft/api/projects/demo-board/open-in-kicad",
            json={},
            headers=await self._mutation_headers(prefix="/pcbdraft"),
        )
        rejected = await self.client.post(
            "/api/projects/demo-board/open-in-kicad",
            json={"path": "/tmp/other.kicad_pcb"},
            headers=await self._mutation_headers(),
        )

        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json(), {"opened": True, "project_id": "demo-board"})
        self.assertNotIn("/", opened.text)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.kicad_opener.opened, ["demo-board"])

    async def test_lifespan_shutdown_does_not_use_asyncio_default_executor(
        self,
    ) -> None:
        with mock.patch(
            "asyncio.to_thread",
            side_effect=AssertionError("default asyncio executor must not be used"),
        ):
            response = await self.client.get("/api/bootstrap")
            async with self.app.router.lifespan_context(self.app):
                pass

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions.shutdown_calls, 1)

    async def test_sse_resumes_by_sequence_and_strips_unsafe_lifecycle_fields(
        self,
    ) -> None:
        self.sessions.start("demo-board", "private user text")
        first = await self.client.get("/api/projects/demo-board/events?after=0&once=1")
        ids = [
            int(line.removeprefix("id: "))
            for line in first.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertEqual(first.status_code, 200)
        self.assertGreaterEqual(len(ids), 3)
        self.assertNotIn("private user text", first.text)
        self.assertNotIn("must not leak", first.text)
        self.assertNotIn("secret", first.text)

        resumed = await self.client.get(
            "/api/projects/demo-board/events?once=1",
            headers={"Last-Event-ID": str(ids[-1])},
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.text, "")

    async def test_artifacts_are_fixed_routes_and_3d_is_generated_only_by_post(
        self,
    ) -> None:
        board = await self.client.get("/api/projects/demo-board/artifacts/board.svg")
        missing_3d = await self.client.get(
            "/api/projects/demo-board/artifacts/board-3d.png"
        )
        unknown = await self.client.get(
            "/api/projects/demo-board/artifacts/../../project.json"
        )
        generated = await self.client.post(
            "/api/projects/demo-board/artifacts/board-3d",
            json={},
            headers=await self._mutation_headers(),
        )

        self.assertEqual(board.status_code, 200)
        self.assertIn("image/svg+xml", board.headers["content-type"])
        self.assertEqual(missing_3d.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(generated.status_code, 202, generated.text)

    async def test_security_headers_cover_static_and_api_responses(self) -> None:
        for path in ("/", "/api/projects"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertIn(
                    "default-src 'self'", response.headers["content-security-policy"]
                )
                self.assertIn(
                    "connect-src 'self'", response.headers["content-security-policy"]
                )


if __name__ == "__main__":
    unittest.main()
