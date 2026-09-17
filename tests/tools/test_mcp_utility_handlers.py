from __future__ import annotations

import ast
import asyncio
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from pcbdraft.tools import mcp_tool, mcp_utility_handlers


class _AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def _run_coroutine_factory(factory, *, timeout):
    del timeout
    return asyncio.run(factory())


class MCPUtilityHandlerCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_identities_remain(self):
        source = Path(mcp_utility_handlers.__file__).read_text(encoding="utf-8")
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertNotIn("pcbdraft.tools.mcp_tool", imports)
        for name in (
            "_make_list_resources_handler",
            "_make_read_resource_handler",
            "_make_list_prompts_handler",
            "_make_get_prompt_handler",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(mcp_tool, name),
                    getattr(mcp_utility_handlers, name),
                )
        self.assertIs(inspect.getmodule(mcp_tool._make_tool_handler), mcp_tool)
        self.assertIs(
            inspect.getmodule(mcp_tool._handle_auth_error_and_retry), mcp_tool
        )

    def test_list_resources_normalizes_results_through_legacy_hooks(self):
        resources = [
            SimpleNamespace(
                uri="file:///guide.md",
                name="guide",
                description="Guide",
                mime_type="text/markdown",
            )
        ]
        server = SimpleNamespace(
            session=SimpleNamespace(list_resources=AsyncMock()),
            _rpc_lock=_AsyncLock(),
        )
        with (
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(mcp_tool, "_mark_server_call_started") as mark_started,
            patch.object(
                mcp_tool,
                "_paginate_full_list",
                new=AsyncMock(return_value=resources),
            ) as paginate,
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=_run_coroutine_factory,
            ),
            patch.object(
                mcp_tool,
                "mcp_field",
                side_effect=lambda value, snake, camel: getattr(value, snake, None),
            ) as field_reader,
        ):
            result = mcp_tool._make_list_resources_handler("docs", 7)({})

        self.assertEqual(
            json.loads(result),
            {
                "resources": [
                    {
                        "uri": "file:///guide.md",
                        "name": "guide",
                        "description": "Guide",
                        "mimeType": "text/markdown",
                    }
                ]
            },
        )
        mark_started.assert_called_once_with(server)
        paginate.assert_awaited_once_with(
            server.session.list_resources,
            "resources",
            "docs",
        )
        field_reader.assert_called_once_with(resources[0], "mime_type", "mimeType")

    def test_read_resource_normalizes_text_and_binary_contents(self):
        text_block = SimpleNamespace(text="<tag>Hello</tag>")
        blob_block = SimpleNamespace(text=None, blob="YWJjZA==")
        session = SimpleNamespace(
            read_resource=AsyncMock(
                return_value=SimpleNamespace(contents=[text_block, blob_block])
            )
        )
        server = SimpleNamespace(session=session, _rpc_lock=_AsyncLock())
        with (
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=_run_coroutine_factory,
            ),
            patch.object(
                mcp_tool,
                "strip_unicode_tags",
                return_value="Hello",
            ) as strip_tags,
            patch.object(
                mcp_tool,
                "_render_mcp_resource_block",
                return_value="DOCUMENT:cached.bin",
            ) as render_resource,
        ):
            result = mcp_tool._make_read_resource_handler("docs", 8)(
                {"uri": "file:///guide.md"}
            )

        self.assertEqual(
            json.loads(result),
            {"result": "Hello\nDOCUMENT:cached.bin"},
        )
        session.read_resource.assert_awaited_once_with("file:///guide.md")
        strip_tags.assert_called_once_with("<tag>Hello</tag>")
        rendered_block, rendered_server = render_resource.call_args.args
        self.assertEqual(rendered_block.type, "resource")
        self.assertIs(rendered_block.resource, blob_block)
        self.assertEqual(rendered_server, "docs")

    def test_prompt_list_and_get_normalize_arguments_and_messages(self):
        prompt = SimpleNamespace(
            name="summarize",
            description="Summarize a document",
            arguments=[
                SimpleNamespace(
                    name="uri",
                    description="Document URI",
                    required=True,
                )
            ],
        )
        prompt_result = SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="user",
                    content=SimpleNamespace(text="<tag>Summarize</tag>"),
                )
            ],
            description="Ready prompt",
        )
        session = SimpleNamespace(
            list_prompts=AsyncMock(),
            get_prompt=AsyncMock(return_value=prompt_result),
        )
        server = SimpleNamespace(session=session, _rpc_lock=_AsyncLock())
        with (
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_paginate_full_list",
                new=AsyncMock(return_value=[prompt]),
            ),
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=_run_coroutine_factory,
            ),
            patch.object(
                mcp_tool,
                "strip_unicode_tags",
                return_value="Summarize",
            ) as strip_tags,
        ):
            listed = mcp_tool._make_list_prompts_handler("docs", 9)({})
            fetched = mcp_tool._make_get_prompt_handler("docs", 9)(
                {"name": "summarize", "arguments": {"uri": "file:///guide.md"}}
            )

        self.assertEqual(
            json.loads(listed),
            {
                "prompts": [
                    {
                        "name": "summarize",
                        "description": "Summarize a document",
                        "arguments": [
                            {
                                "name": "uri",
                                "description": "Document URI",
                                "required": True,
                            }
                        ],
                    }
                ]
            },
        )
        self.assertEqual(
            json.loads(fetched),
            {
                "messages": [{"role": "user", "content": "Summarize"}],
                "description": "Ready prompt",
            },
        )
        session.get_prompt.assert_awaited_once_with(
            "summarize",
            arguments={"uri": "file:///guide.md"},
        )
        strip_tags.assert_called_once_with("<tag>Summarize</tag>")

    def test_utility_failure_delegates_to_legacy_recovery_hook(self):
        server = SimpleNamespace(
            session=SimpleNamespace(list_resources=AsyncMock()),
            _rpc_lock=_AsyncLock(),
        )
        recovered = '{"result":"recovered"}'
        with (
            patch.object(
                mcp_tool,
                "_get_connected_server_for_call",
                return_value=server,
            ),
            patch.object(
                mcp_tool,
                "_run_on_mcp_loop",
                side_effect=RuntimeError("transport failed"),
            ),
            patch.object(
                mcp_tool,
                "_handle_auth_error_and_retry",
                return_value=recovered,
            ) as auth_recovery,
            patch.object(
                mcp_tool,
                "_handle_session_expired_and_retry",
            ) as session_recovery,
        ):
            result = mcp_tool._make_list_resources_handler("docs", 10)({})

        self.assertEqual(result, recovered)
        auth_recovery.assert_called_once_with(
            "docs",
            ANY,
            ANY,
            "resources/list",
        )
        session_recovery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
