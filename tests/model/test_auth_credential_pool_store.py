"""Focused coverage for extracted credential-pool auth-store state."""

from __future__ import annotations

import ast
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import call, patch

from pcbdraft.model import auth, auth_credential_pool_store, credential_pool


class AuthCredentialPoolStoreTests(unittest.TestCase):
    def test_auth_reexports_same_objects_without_reverse_import(self) -> None:
        for name in (
            "_POOL_STATUS_FIELDS",
            "read_credential_pool",
            "_merge_disk_cooldown_state",
            "write_credential_pool",
            "suppress_credential_source",
            "is_source_suppressed",
            "unsuppress_credential_source",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auth, name),
                    getattr(auth_credential_pool_store, name),
                )

        tree = ast.parse(
            Path(auth_credential_pool_store.__file__).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.model.auth", imports)

    def test_read_pool_merges_global_fallback_per_provider(self) -> None:
        profile_entry = {"id": "profile"}
        global_a = {"id": "global-a"}
        global_b = {"id": "global-b"}
        global_empty = {"id": "global-empty"}
        profile_store = {
            "credential_pool": {
                "provider-a": [profile_entry],
                "provider-empty": [],
            }
        }
        global_store = {
            "credential_pool": {
                "provider-a": [global_a],
                "provider-b": [global_b],
                "provider-empty": [global_empty],
                "invalid": "not-a-list",
            }
        }
        with (
            patch.object(auth, "_load_auth_store", return_value=profile_store) as load,
            patch.object(
                auth, "_load_global_auth_store", return_value=global_store
            ) as load_global,
        ):
            merged = auth.read_credential_pool()
            provider_a = auth.read_credential_pool("provider-a")
            provider_b = auth.read_credential_pool("provider-b")
            missing = auth.read_credential_pool("missing")

        self.assertEqual(merged["provider-a"], [profile_entry])
        self.assertEqual(merged["provider-b"], [global_b])
        self.assertEqual(merged["provider-empty"], [global_empty])
        self.assertNotIn("invalid", merged)
        self.assertEqual(provider_a, [profile_entry])
        self.assertEqual(provider_b, [global_b])
        self.assertEqual(missing, [])
        self.assertEqual(load.call_count, 4)
        self.assertEqual(load_global.call_count, 4)

    def test_write_pool_preserves_concurrent_entries_and_old_patch_paths(self) -> None:
        store = {
            "providers": {},
            "credential_pool": {
                "provider": [
                    {"id": "same", "last_status": "disk"},
                    {"id": "concurrent", "secret": "disk"},
                    {"id": "removed", "secret": "removed"},
                ]
            },
        }
        destination = Path("/tmp/test-auth-pool.json")

        def sanitize(entry, provider_id):
            return {**entry, "sanitized_for": provider_id}

        def merge(entry, disk_entry, provider_id):
            return {
                **entry,
                "disk_status": disk_entry.get("last_status"),
                "merged_for": provider_id,
            }

        with (
            patch.object(auth, "_auth_store_lock", return_value=nullcontext()) as lock,
            patch.object(auth, "_load_auth_store", return_value=store),
            patch.object(auth, "_save_auth_store", return_value=destination) as save,
            patch.object(
                auth,
                "sanitize_borrowed_credential_payload",
                side_effect=sanitize,
            ) as sanitize_payload,
            patch.object(
                auth,
                "_merge_disk_cooldown_state",
                side_effect=merge,
            ) as merge_status,
        ):
            result = auth.write_credential_pool(
                "provider",
                [{"id": "same", "secret": "memory"}],
                removed_ids=["removed"],
            )

        self.assertEqual(result, destination)
        self.assertEqual(
            store["credential_pool"]["provider"],
            [
                {
                    "id": "same",
                    "secret": "memory",
                    "sanitized_for": "provider",
                    "disk_status": "disk",
                    "merged_for": "provider",
                },
                {
                    "id": "concurrent",
                    "secret": "disk",
                    "sanitized_for": "provider",
                },
            ],
        )
        lock.assert_called_once_with()
        save.assert_called_once_with(store)
        self.assertEqual(sanitize_payload.call_count, 2)
        merge_status.assert_called_once()

    def test_cooldown_merge_uses_legacy_fields_and_time_patch_paths(self) -> None:
        memory = {
            "id": "one",
            "access_token": "same",
            "last_status": None,
            "last_status_at": "10",
            "last_error_code": None,
        }
        disk = {
            "id": "one",
            "access_token": "same",
            "last_status": credential_pool.STATUS_EXHAUSTED,
            "last_status_at": "20",
            "last_error_code": "429",
        }
        pooled = object()
        with (
            patch.object(
                credential_pool,
                "_parse_absolute_timestamp",
                side_effect=lambda value: float(value),
            ),
            patch.object(
                credential_pool.PooledCredential, "from_dict", return_value=pooled
            ),
            patch.object(credential_pool, "_exhausted_until", return_value=150.0),
            patch.object(auth.time, "time", return_value=100.0),
            patch.object(
                auth, "_POOL_STATUS_FIELDS", ("last_status", "last_error_code")
            ),
        ):
            merged = auth._merge_disk_cooldown_state(memory, disk, "provider")

        self.assertEqual(
            merged,
            {
                **memory,
                "last_status": credential_pool.STATUS_EXHAUSTED,
                "last_error_code": "429",
            },
        )

        with (
            patch.object(
                credential_pool,
                "_parse_absolute_timestamp",
                side_effect=lambda value: float(value),
            ),
            patch.object(
                credential_pool.PooledCredential, "from_dict", return_value=pooled
            ),
            patch.object(credential_pool, "_exhausted_until", return_value=150.0),
            patch.object(auth.time, "time", return_value=200.0),
        ):
            self.assertIs(
                auth._merge_disk_cooldown_state(memory, disk, "provider"), memory
            )

    def test_suppression_migrates_legacy_sources_and_cleans_empty_state(self) -> None:
        store = {
            "providers": {},
            "suppressed_sources": {"anthropic": {"hermes_pkce": True}},
        }
        with (
            patch.object(auth, "_auth_store_lock", return_value=nullcontext()),
            patch.object(auth, "_load_auth_store", return_value=store),
            patch.object(
                auth, "_save_auth_store", return_value=Path("auth.json")
            ) as save,
        ):
            self.assertTrue(auth.is_source_suppressed("anthropic", "pcbdraft_pkce"))
            auth.suppress_credential_source("anthropic", "pcbdraft_pkce")
            self.assertEqual(
                store["suppressed_sources"]["anthropic"], ["pcbdraft_pkce"]
            )
            self.assertTrue(
                auth.unsuppress_credential_source("anthropic", "hermes_pkce")
            )
            self.assertNotIn("suppressed_sources", store)
            self.assertFalse(
                auth.unsuppress_credential_source("anthropic", "pcbdraft_pkce")
            )

        self.assertEqual(save.call_args_list, [call(store), call(store)])

    def test_suppression_read_failure_still_fails_open(self) -> None:
        with patch.object(auth, "_load_auth_store", side_effect=OSError("unreadable")):
            self.assertFalse(auth.is_source_suppressed("provider", "source"))


if __name__ == "__main__":
    unittest.main()
