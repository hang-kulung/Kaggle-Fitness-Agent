import asyncio
import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_adk_test_stubs():
    google_mod = types.ModuleType("google")
    adk_mod = types.ModuleType("google.adk")
    adk_memory_mod = types.ModuleType("google.adk.memory")
    adk_sessions_mod = types.ModuleType("google.adk.sessions")
    genai_mod = types.ModuleType("google.genai")

    tools_mod = types.ModuleType("google.adk.tools")
    base_memory_mod = types.ModuleType("google.adk.memory.base_memory_service")
    session_mod = types.ModuleType("google.adk.sessions.session")
    genai_types_mod = types.ModuleType("google.genai.types")

    class FunctionTool:
        def __init__(self, fn):
            self.fn = fn

    class BaseMemoryService:
        pass

    class MemoryEntry:
        def __init__(self, content):
            self.content = content

    class SearchMemoryResponse:
        def __init__(self, memories):
            self.memories = memories

    class Session:
        pass

    class Part:
        def __init__(self, text=None):
            self.text = text

    class Content:
        def __init__(self, role=None, parts=None):
            self.role = role
            self.parts = parts or []

    tools_mod.FunctionTool = FunctionTool
    base_memory_mod.BaseMemoryService = BaseMemoryService
    base_memory_mod.MemoryEntry = MemoryEntry
    base_memory_mod.SearchMemoryResponse = SearchMemoryResponse
    session_mod.Session = Session
    genai_types_mod.Part = Part
    genai_types_mod.Content = Content
    genai_mod.types = genai_types_mod

    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.adk", adk_mod)
    sys.modules.setdefault("google.adk.memory", adk_memory_mod)
    sys.modules.setdefault("google.adk.tools", tools_mod)
    sys.modules.setdefault("google.adk.memory.base_memory_service", base_memory_mod)
    sys.modules.setdefault("google.adk.sessions", adk_sessions_mod)
    sys.modules.setdefault("google.adk.sessions.session", session_mod)
    sys.modules.setdefault("google.genai", genai_mod)
    sys.modules.setdefault("google.genai.types", genai_types_mod)


_install_adk_test_stubs()

import auth
from memory_service import StructuredMemoryService
from plan_manager import _make_tool_functions


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DATA_DIR"] = self.tmp.name
        importlib.reload(auth)

    def test_register_login_and_reject_unsafe_usernames(self):
        success, _ = auth.register_user("user_1", "secret1")
        self.assertTrue(success)

        success, _ = auth.login_user("user_1", "secret1")
        self.assertTrue(success)

        success, _ = auth.register_user("../bad", "secret1")
        self.assertFalse(success)

    def test_legacy_sha256_user_is_upgraded_after_login(self):
        salt = "abc123"
        password = "secret1"
        users = {
            "legacy": {
                "salt": salt,
                "password_hash": hashlib.sha256(f"{salt}{password}".encode()).hexdigest(),
            }
        }
        Path(auth.USERS_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(auth.USERS_FILE).write_text(json.dumps(users))

        success, _ = auth.login_user("legacy", password)
        self.assertTrue(success)

        upgraded = json.loads(Path(auth.USERS_FILE).read_text())["legacy"]
        self.assertEqual(upgraded["algorithm"], "pbkdf2_sha256")
        self.assertIn("iterations", upgraded)


class StorageIsolationTests(unittest.TestCase):
    def test_workout_tools_write_to_separate_user_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_a = Path(tmp) / "user_a"
            user_b = Path(tmp) / "user_b"
            funcs_a = _make_tool_functions(
                str(user_a / "workout_plan.json"),
                str(user_a / "memory"),
            )
            funcs_b = _make_tool_functions(
                str(user_b / "workout_plan.json"),
                str(user_b / "memory"),
            )
            save_a = funcs_a[0]
            save_b = funcs_b[0]

            save_a({"goal": "strength", "days": {}})
            save_b({"goal": "mobility", "days": {}})

            plan_a = json.loads((user_a / "workout_plan.json").read_text())
            plan_b = json.loads((user_b / "workout_plan.json").read_text())
            self.assertEqual(plan_a["goal"], "strength")
            self.assertEqual(plan_b["goal"], "mobility")

    def test_memory_services_keep_independent_data_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_a = Path(tmp) / "user_a" / "memory"
            user_b = Path(tmp) / "user_b" / "memory"
            service_a = StructuredMemoryService(str(user_a))
            service_b = StructuredMemoryService(str(user_b))

            (user_a / "user_profile.json").write_text(json.dumps({"goal": "strength"}))
            (user_b / "user_profile.json").write_text(json.dumps({"goal": "mobility"}))

            async def read_goals():
                result_a = await service_a.search_memory(
                    app_name="app", user_id="user_a", query="goal"
                )
                result_b = await service_b.search_memory(
                    app_name="app", user_id="user_b", query="goal"
                )
                text_a = result_a.memories[0].content.parts[0].text
                text_b = result_b.memories[0].content.parts[0].text
                return text_a, text_b

            text_a, text_b = asyncio.run(read_goals())
            self.assertIn("strength", text_a)
            self.assertIn("mobility", text_b)


if __name__ == "__main__":
    unittest.main()
