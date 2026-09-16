from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pcbdraft.core import runtime_environment
from pcbdraft.tools import mcp_content, mcp_tool


class MCPContentCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_content_helpers(self):
        names = (
            "_is_reserved_mcp_meta_key",
            "_strip_reserved_meta_keys",
            "_mcp_image_extension_for_mime_type",
            "_cache_mcp_image_block",
            "_MCP_RESOURCE_MAX_BYTES",
            "_MCP_RESOURCE_MAX_B64_CHARS",
            "_mcp_resource_filename",
            "_cache_mcp_audio_block",
            "_render_mcp_resource_block",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(mcp_tool, name),
                    getattr(mcp_content, name),
                )

    def test_reserved_metadata_filter_preserves_vendor_namespaces(self):
        self.assertEqual(
            mcp_content._strip_reserved_meta_keys(
                {
                    "tools.mcp.example/detail": "hidden",
                    "modelcontextprotocol.io/internal": "hidden",
                    "com.example.mcp/detail": "visible",
                    "plain": 1,
                }
            ),
            {"com.example.mcp/detail": "visible", "plain": 1},
        )


class MCPContentRenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.runtime_home = Path(temporary)
        token = runtime_environment.set_runtime_home_override(self.runtime_home)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)

    def test_image_and_audio_blocks_materialize_with_mime_extensions(self):
        payload = base64.b64encode(b"media-bytes").decode("ascii")

        image_marker = mcp_content._cache_mcp_image_block(
            SimpleNamespace(data=payload, mimeType="image/jpeg; charset=binary")
        )
        image_path = Path(image_marker.removeprefix("MEDIA:"))
        self.assertEqual(image_path.suffix, ".jpg")
        self.assertEqual(image_path.read_bytes(), b"media-bytes")
        self.assertTrue(
            image_path.is_relative_to(self.runtime_home / "cache" / "images")
        )

        audio_marker = mcp_content._cache_mcp_audio_block(
            SimpleNamespace(data=payload, mime_type="audio/wav")
        )
        audio_path = Path(audio_marker.removeprefix("MEDIA:"))
        self.assertEqual(audio_path.suffix, ".wav")
        self.assertEqual(audio_path.read_bytes(), b"media-bytes")
        self.assertTrue(
            audio_path.is_relative_to(self.runtime_home / "cache" / "audio")
        )

    def test_resource_link_uses_sanitized_prefixed_reader_name(self):
        block = SimpleNamespace(
            type="resource_link",
            uri="mcp://docs/manual.pdf",
            name="manual",
            mimeType="application/pdf",
        )
        rendered = mcp_content._render_mcp_resource_block(block, "docs server/unsafe")
        self.assertEqual(
            rendered,
            "[MCP resource link: uri=mcp://docs/manual.pdf, name=manual, "
            "mimeType=application/pdf — fetch it with "
            "mcp__docs_server_unsafe__read_resource]",
        )

    def test_embedded_resource_uses_safe_local_document_path(self):
        resource = SimpleNamespace(
            blob=base64.b64encode(b"document-bytes").decode("ascii"),
            uri="https://example.test/../../%0Abad%5Cname.txt",
            mimeType="text/plain",
        )
        rendered = mcp_content._render_mcp_resource_block(
            SimpleNamespace(type="resource", resource=resource)
        )
        prefix = "[MCP resource saved to "
        self.assertTrue(rendered.startswith(prefix), rendered)
        cached_path = Path(rendered[len(prefix) :].split(" (", 1)[0])
        self.assertTrue(
            cached_path.is_relative_to(self.runtime_home / "cache" / "documents")
        )
        self.assertNotIn("\n", cached_path.name)
        self.assertNotIn("\\", cached_path.name)
        self.assertEqual(cached_path.read_bytes(), b"document-bytes")
        self.assertIn("(text/plain, 14 bytes)", rendered)

    def test_embedded_text_and_non_media_blocks_remain_inline(self):
        text_block = SimpleNamespace(
            type="resource",
            resource=SimpleNamespace(text="plain resource text"),
        )
        self.assertEqual(
            mcp_content._render_mcp_resource_block(text_block),
            "plain resource text",
        )
        self.assertEqual(
            mcp_content._cache_mcp_image_block(
                SimpleNamespace(data="eA==", mimeType="text/plain")
            ),
            "",
        )
        self.assertEqual(
            mcp_content._cache_mcp_audio_block(
                SimpleNamespace(data="eA==", mimeType="application/octet-stream")
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
