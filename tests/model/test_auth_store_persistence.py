from __future__ import annotations

import ast
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from pcbdraft.model import auth, auth_store_persistence


class AuthStorePersistenceCompatibilityTests(unittest.TestCase):
    def test_extracted_module_has_no_reverse_import_and_legacy_symbols_remain(self):
        source = Path(auth_store_persistence.__file__).read_text(encoding="utf-8")
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
        self.assertNotIn("pcbdraft.model.auth", imports)

        for name in (
            "_auth_file_path",
            "_global_auth_file_path",
            "_load_global_auth_store",
            "_auth_lock_path",
            "_same_path",
            "_auth_lock_holder_for",
            "_file_lock",
            "_auth_store_lock",
            "_load_auth_store",
            "_save_auth_store",
            "_load_provider_state_with_source",
            "_provider_state_transaction",
            "_load_provider_state",
            "_save_provider_state",
            "_save_provider_state_to_source",
            "_store_provider_state",
            "_persist_provider_state_to_store",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(auth, name)))

    def test_legacy_atomic_save_uses_patched_path_and_replace_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "profile" / "auth.json"
            replace = Mock(side_effect=os.replace)
            store = {"providers": {"docs": {"token": "value"}}}

            with (
                patch.object(auth, "_auth_file_path", return_value=target),
                patch.object(auth, "atomic_replace", replace),
            ):
                saved = auth._save_auth_store(store)

            self.assertEqual(saved, target)
            replace.assert_called_once()
            temporary, destination = replace.call_args.args
            self.assertEqual(destination, target)
            self.assertFalse(temporary.exists())
            self.assertEqual(list(target.parent.glob("auth.json.tmp.*")), [])
            disk = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(disk["providers"]["docs"]["token"], "value")
            self.assertEqual(disk["version"], auth.AUTH_STORE_VERSION)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_legacy_load_preserves_corrupt_file_and_returns_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "auth.json"
            target.write_text("{broken", encoding="utf-8")

            loaded = auth._load_auth_store(target)

            self.assertEqual(
                loaded,
                {"version": auth.AUTH_STORE_VERSION, "providers": {}},
            )
            self.assertEqual(target.with_suffix(".json.corrupt").read_text(), "{broken")

    def test_legacy_file_lock_keeps_per_thread_reentrancy(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = threading.local()
            lock_path = Path(tmp) / "auth.lock"
            with (
                patch.object(auth, "fcntl", None),
                patch.object(auth, "msvcrt", None),
                auth._file_lock(lock_path, holder, 1.0, "timeout"),
            ):
                self.assertEqual(holder.depth, 1)
                with auth._file_lock(lock_path, holder, 1.0, "timeout"):
                    self.assertEqual(holder.depth, 2)
                self.assertEqual(holder.depth, 1)
            self.assertEqual(holder.depth, 0)

    def test_store_lock_resolves_legacy_patch_points_at_call_time(self):
        active = Path("profile/auth.json")
        holder = threading.local()
        file_lock = Mock(return_value=nullcontext())
        with (
            patch.object(auth, "_auth_file_path", return_value=active),
            patch.object(auth, "_auth_lock_path", return_value=Path("patched.lock")),
            patch.object(auth, "_auth_lock_holder_for", return_value=holder),
            patch.object(auth, "_file_lock", file_lock),
            auth._auth_store_lock(4.5),
        ):
            pass

        file_lock.assert_called_once_with(
            Path("patched.lock"),
            holder,
            4.5,
            "Timed out waiting for auth store lock",
        )

    def test_profile_state_falls_back_to_global_store_and_reports_source(self):
        global_path = Path("global/auth.json")
        global_store = {"providers": {"docs": {"token": "global"}}}
        with (
            patch.object(auth, "_global_auth_file_path", return_value=global_path),
            patch.object(
                auth,
                "_load_global_auth_store",
                return_value=global_store,
            ) as load_global,
        ):
            state, source = auth._load_provider_state_with_source(
                {"providers": {}}, "docs"
            )

        load_global.assert_called_once_with()
        self.assertEqual(state, {"token": "global"})
        self.assertEqual(source, global_path)
        self.assertIsNot(state, global_store["providers"]["docs"])

    def test_profile_state_shadows_global_without_loading_it(self):
        profile_path = Path("profile/auth.json")
        with (
            patch.object(auth, "_auth_file_path", return_value=profile_path),
            patch.object(auth, "_load_global_auth_store") as load_global,
        ):
            state, source = auth._load_provider_state_with_source(
                {"providers": {"docs": {"token": "profile"}}},
                "docs",
            )

        load_global.assert_not_called()
        self.assertEqual(state, {"token": "profile"})
        self.assertEqual(source, profile_path)

    def test_transaction_relocks_and_rereads_distinct_global_source(self):
        active_path = Path("profile/auth.json")
        global_path = Path("global/auth.json")
        locked: list[Path | None] = []

        @contextmanager
        def fake_lock(*_args, target_path=None, **_kwargs):
            locked.append(target_path)
            yield

        profile_store = {"providers": {}}
        global_store = {"providers": {"docs": {"token": "fresh"}}}

        def load_store(path=None):
            return global_store if path == global_path else profile_store

        with (
            patch.object(auth, "_auth_store_lock", fake_lock),
            patch.object(auth, "_load_auth_store", side_effect=load_store),
            patch.object(
                auth,
                "_load_provider_state_with_source",
                return_value=({"token": "stale"}, global_path),
            ),
            patch.object(auth, "_auth_file_path", return_value=active_path),
            auth._provider_state_transaction("docs") as transaction,
        ):
            auth_store, state, source = transaction

        self.assertIs(auth_store, profile_store)
        self.assertEqual(state, {"token": "fresh"})
        self.assertEqual(source, global_path)
        self.assertEqual(locked, [None, global_path])

    def test_source_writeback_uses_legacy_active_and_global_hooks(self):
        active_path = Path("profile/auth.json")
        global_path = Path("global/auth.json")
        store = {"providers": {}}
        state = {"token": "rotated"}

        with (
            patch.object(auth, "_auth_file_path", return_value=active_path),
            patch.object(auth, "_same_path", return_value=True),
            patch.object(auth, "_save_provider_state") as save_state,
            patch.object(auth, "_save_auth_store") as save_store,
            patch.object(auth, "_persist_provider_state_to_store") as persist,
        ):
            auth._save_provider_state_to_source(store, "docs", state, active_path)
        save_state.assert_called_once_with(store, "docs", state)
        save_store.assert_called_once_with(store)
        persist.assert_not_called()

        with (
            patch.object(auth, "_auth_file_path", return_value=active_path),
            patch.object(auth, "_same_path", return_value=False),
            patch.object(auth, "_persist_provider_state_to_store") as persist,
        ):
            auth._save_provider_state_to_source(store, "docs", state, global_path)
        persist.assert_called_once_with("docs", state, global_path, set_active=True)

    def test_targeted_provider_persistence_uses_patched_store_hooks(self):
        target = Path("global/auth.json")
        stored = {"providers": {"other": {}}}
        state = {"token": "rotated"}

        @contextmanager
        def fake_lock(*_args, **_kwargs):
            yield

        with (
            patch.object(auth, "_auth_store_lock", wraps=fake_lock) as lock,
            patch.object(auth, "_load_auth_store", return_value=stored) as load,
            patch.object(auth, "_store_provider_state") as store_provider,
            patch.object(
                auth,
                "_save_auth_store",
                return_value=target,
            ) as save,
        ):
            result = auth._persist_provider_state_to_store(
                "docs", state, target, set_active=True
            )

        self.assertEqual(result, target)
        lock.assert_called_once_with(target_path=target)
        load.assert_called_once_with(target)
        store_provider.assert_called_once_with(
            stored,
            "docs",
            state,
            set_active=True,
        )
        save.assert_called_once_with(stored, target_path=target)


if __name__ == "__main__":
    unittest.main()
