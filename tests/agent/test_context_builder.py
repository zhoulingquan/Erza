"""Tests for ContextBuilder — system prompt and message assembly."""

from pathlib import Path

from erza.agent.context import ContextBuilder
from erza.session.goal_state import GOAL_STATE_KEY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _builder(tmp_path: Path, **kw) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path, **kw)


# ---------------------------------------------------------------------------
# _build_runtime_context (static)
# ---------------------------------------------------------------------------


class TestBuildRuntimeContext:
    def test_time_only(self):
        ctx = ContextBuilder._build_runtime_context(None, None)
        assert "[Runtime Context" in ctx
        assert "[/Runtime Context]" in ctx
        assert "Current Time:" in ctx
        assert "Channel:" not in ctx

    def test_with_channel_and_chat_id(self):
        ctx = ContextBuilder._build_runtime_context("telegram", "chat123")
        assert "Channel: telegram" in ctx
        assert "Chat ID: chat123" in ctx

    def test_with_sender_id(self):
        ctx = ContextBuilder._build_runtime_context("cli", "direct", sender_id="user1")
        assert "Sender ID: user1" in ctx

    def test_with_timezone(self):
        ctx = ContextBuilder._build_runtime_context(None, None, timezone="Asia/Shanghai")
        assert "Current Time:" in ctx

    def test_no_channel_no_chat_id_omits_both(self):
        ctx = ContextBuilder._build_runtime_context(None, None)
        assert "Channel:" not in ctx
        assert "Chat ID:" not in ctx

    def test_no_sender_id_omits(self):
        ctx = ContextBuilder._build_runtime_context("cli", "direct")
        assert "Sender ID:" not in ctx


# ---------------------------------------------------------------------------
# _merge_message_content (static)
# ---------------------------------------------------------------------------


class TestMergeMessageContent:
    def test_str_plus_str(self):
        result = ContextBuilder._merge_message_content("hello", "world")
        assert result == "hello\n\nworld"

    def test_empty_left_plus_str(self):
        result = ContextBuilder._merge_message_content("", "world")
        assert result == "world"

    def test_list_plus_list(self):
        left = [{"type": "text", "text": "a"}]
        right = [{"type": "text", "text": "b"}]
        result = ContextBuilder._merge_message_content(left, right)
        assert len(result) == 2
        assert result[0]["text"] == "a"
        assert result[1]["text"] == "b"

    def test_str_plus_list(self):
        right = [{"type": "text", "text": "b"}]
        result = ContextBuilder._merge_message_content("hello", right)
        assert len(result) == 2
        assert result[0]["text"] == "hello"
        assert result[1]["text"] == "b"

    def test_list_plus_str(self):
        left = [{"type": "text", "text": "a"}]
        result = ContextBuilder._merge_message_content(left, "world")
        assert len(result) == 2
        assert result[0]["text"] == "a"
        assert result[1]["text"] == "world"

    def test_none_plus_str(self):
        result = ContextBuilder._merge_message_content(None, "hello")
        assert result == [{"type": "text", "text": "hello"}]

    def test_str_plus_none(self):
        result = ContextBuilder._merge_message_content("hello", None)
        assert result == [{"type": "text", "text": "hello"}]

    def test_none_plus_none(self):
        result = ContextBuilder._merge_message_content(None, None)
        assert result == []

    def test_list_items_not_dicts_wrapped(self):
        result = ContextBuilder._merge_message_content(["raw_item"], None)
        assert result == [{"type": "text", "text": "raw_item"}]


# ---------------------------------------------------------------------------
# _load_bootstrap_files
# ---------------------------------------------------------------------------


class TestLoadBootstrapFiles:
    def test_no_bootstrap_files(self, tmp_path):
        builder = _builder(tmp_path)
        assert builder._load_bootstrap_files() == ""

    def test_agents_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Be helpful.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "## AGENTS.md" in result
        assert "Be helpful." in result

    def test_multiple_bootstrap_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Rules.", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("Soul.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "## AGENTS.md" in result
        assert "## SOUL.md" in result
        assert "Rules." in result
        assert "Soul." in result

    def test_all_bootstrap_files(self, tmp_path):
        for name in ContextBuilder.BOOTSTRAP_FILES:
            (tmp_path / name).write_text(f"Content of {name}", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        for name in ContextBuilder.BOOTSTRAP_FILES:
            assert f"## {name}" in result

    def test_legacy_tools_md_is_not_bootstrapped(self, tmp_path):
        (tmp_path / "TOOLS.md").write_text("workspace tool notes", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "TOOLS.md" not in result
        assert "workspace tool notes" not in result

    def test_utf8_content(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("用中文回复", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "用中文回复" in result


# ---------------------------------------------------------------------------
# _is_template_content (static)
# ---------------------------------------------------------------------------


class TestIsTemplateContent:
    def test_nonexistent_template_returns_false(self):
        assert ContextBuilder._is_template_content("anything", "nonexistent/path.md") is False

    def test_content_matching_template(self):
        from importlib.resources import files as pkg_files

        tpl = pkg_files("erza") / "templates" / "memory" / "POLICY.md"
        original = tpl.read_text(encoding="utf-8")
        assert ContextBuilder._is_template_content(original, "memory/POLICY.md") is True

    def test_modified_content_returns_false(self):
        assert ContextBuilder._is_template_content("totally different", "memory/POLICY.md") is False


# ---------------------------------------------------------------------------
# Bundled bootstrap templates
# ---------------------------------------------------------------------------


class TestBundledToolContract:
    def test_tool_contract_balances_general_and_coding_workflows(self):
        from importlib.resources import files as pkg_files

        tpl = pkg_files("erza") / "templates" / "agent" / "tool_contract.md"
        content = tpl.read_text(encoding="utf-8")

        assert "## General Tool Contract" in content
        assert "Use the narrowest structured tool" in content
        assert "Do not use `exec` as a universal workaround" in content
        assert "## File and Coding Workflows" in content
        assert "apply_patch" in content
        assert "## Web and External Information" in content
        assert "## Messaging and Media" in content
        assert "## Scheduling and Background Work" in content
        assert "pure coding" not in content.lower()

    def test_tool_contract_is_injected_without_workspace_file(self, tmp_path):
        builder = _builder(tmp_path)
        prompt = builder.build_system_prompt()

        assert "# Tool Usage Notes" in prompt
        assert "## General Tool Contract" in prompt
        assert "Do not use `exec` as a universal workaround" in prompt


# ---------------------------------------------------------------------------
# _build_user_content
# ---------------------------------------------------------------------------


class TestBuildUserContent:
    def test_no_media_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", None)
        assert result == "hello"

    def test_empty_media_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [])
        assert result == "hello"

    def test_nonexistent_media_file_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", ["/nonexistent/image.png"])
        assert result == "hello"

    def test_non_image_file_returns_string(self, tmp_path):
        txt = tmp_path / "doc.txt"
        txt.write_text("not an image", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(txt)])
        assert result == "hello"

    def test_valid_image_returns_list(self, tmp_path):
        png = tmp_path / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(png)])
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert result[1]["type"] == "text"
        assert result[1]["text"] == "hello"

    def test_image_meta_includes_path(self, tmp_path):
        png = tmp_path / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(png)])
        assert "_meta" in result[0]
        assert "path" in result[0]["_meta"]


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_returns_nonempty_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_identity_section(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "workspace" in result.lower() or "python" in result.lower()

    def test_includes_bootstrap_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Be helpful and concise.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "Be helpful and concise." in result

    def test_summary_replayed_from_history_not_system_prompt(self, tmp_path):
        """W10-C3: archived summaries are user messages in history, not prompt sections."""
        builder = _builder(tmp_path)
        history = [
            {
                "role": "user",
                "content": "[Archived Context Summary]\n\nEarlier chat about Python.",
                "_archived_summary": True,
            },
        ]
        messages = builder.build_messages(history, "continue")
        joined = "\n".join(str(m["content"]) for m in messages)
        assert "Earlier chat about Python." in joined
        # And the system prompt itself must not carry a summary section anymore.
        assert "[Archived Context Summary]" not in builder.build_system_prompt()

    def test_sections_separated_by_separator(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Rules.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "\n\n---\n\n" in result

    def test_no_bootstrap_no_summary(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "## AGENTS.md" not in result
        assert "[Archived Context Summary]" not in result


# ---------------------------------------------------------------------------
# _enforce_injection_budget（Phase 4：65K token 注入预算上限）
# ---------------------------------------------------------------------------


class TestEnforceInjectionBudget:
    """65K token injection budget: drop/truncate by priority ladder.

    W10-C2: dynamic sections (memory/history/notes) no longer live in the
    system prompt. W10-C3: the SUMMARY section was removed as well, so the
    ladder only guards CRITICAL/SKILLS.
    """

    def test_under_budget_unchanged(self):
        parts = [
            (ContextBuilder._PRIORITY_CRITICAL, "identity"),
            (ContextBuilder._PRIORITY_SKILLS_ACTIVE, "active skills"),
        ]
        result = ContextBuilder._enforce_injection_budget(parts)
        assert len(result) == 2
        assert all(p in result for p in parts)

    def test_skills_list_and_subagent_dropped_first(self):
        big_blob = "x" * 300_000  # ~75K tokens
        parts = [
            (ContextBuilder._PRIORITY_CRITICAL, "identity"),
            (ContextBuilder._PRIORITY_SKILLS_ACTIVE, big_blob),
            (ContextBuilder._PRIORITY_SKILLS_LIST, "skills summary"),
            (ContextBuilder._PRIORITY_SUBAGENT, "subagent list"),
        ]
        result = ContextBuilder._enforce_injection_budget(parts)
        priorities = {p[0] for p in result}
        assert ContextBuilder._PRIORITY_SKILLS_LIST not in priorities
        assert ContextBuilder._PRIORITY_SUBAGENT not in priorities
        assert ContextBuilder._PRIORITY_SKILLS_ACTIVE in priorities

    def test_skills_active_truncated_not_dropped(self):
        big_skills = "s" * 300_000
        parts = [
            (ContextBuilder._PRIORITY_CRITICAL, "identity"),
            (ContextBuilder._PRIORITY_SKILLS_ACTIVE, big_skills),
        ]
        result = ContextBuilder._enforce_injection_budget(parts)
        active = [p for p in result if p[0] == ContextBuilder._PRIORITY_SKILLS_ACTIVE]
        assert len(active) == 1
        assert len(active[0][1]) < len(big_skills)

    def test_critical_never_dropped(self):
        big_blob = "x" * 1_000_000  # ~250K tokens
        parts = [
            (ContextBuilder._PRIORITY_CRITICAL, big_blob),
            (ContextBuilder._PRIORITY_SKILLS_ACTIVE, "active skills"),
        ]
        result = ContextBuilder._enforce_injection_budget(parts)
        critical_parts = [p for p in result if p[0] == ContextBuilder._PRIORITY_CRITICAL]
        assert len(critical_parts) == 1
        assert critical_parts[0][1] == big_blob

    def test_estimate_tokens_heuristic(self):
        """token 估算启发式：chars/4 下界。"""
        assert ContextBuilder._estimate_tokens("") == 1
        assert ContextBuilder._estimate_tokens("hi") == 1  # 2 chars → 0.5 token → max(1, 0) = 1
        assert ContextBuilder._estimate_tokens("hello world") == 2  # 11 chars → 2 tokens
        # 4000 字符 → 1000 tokens
        assert ContextBuilder._estimate_tokens("x" * 4000) == 1000


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------


class TestBuildMessages:
    def test_basic_empty_history(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages([], "hello")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "hello" in str(messages[1]["content"])

    def test_runtime_context_injected(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages([], "hello", channel="cli", chat_id="direct")
        user_msg = str(messages[-1]["content"])
        assert "[Runtime Context" in user_msg
        assert "hello" in user_msg

    def test_session_metadata_injects_active_goal_state(self, tmp_path):
        builder = _builder(tmp_path)
        meta = {
            GOAL_STATE_KEY: {"status": "active", "objective": "Finish docs migration."},
        }
        messages = builder.build_messages(
            [],
            "hi",
            channel="cli",
            chat_id="x",
            session_metadata=meta,
        )
        user_msg = str(messages[-1]["content"])
        assert "Goal (active):" in user_msg
        assert "Finish docs migration." in user_msg

    def test_goal_state_does_not_leak_without_session_metadata(self, tmp_path):
        builder = _builder(tmp_path)
        other_session_meta = {
            GOAL_STATE_KEY: {"status": "active", "objective": "Other chat goal."},
        }

        with_goal = builder.build_messages(
            [],
            "hi",
            channel="websocket",
            chat_id="chat-a",
            session_metadata=other_session_meta,
        )
        without_goal = builder.build_messages(
            [],
            "hi",
            channel="websocket",
            chat_id="chat-b",
            session_metadata={},
        )

        assert "Other chat goal." in str(with_goal[-1]["content"])
        assert "Other chat goal." not in str(without_goal[-1]["content"])
        assert "Goal (active):" not in str(without_goal[-1]["content"])

    def test_current_runtime_lines_are_injected(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            [],
            "please use @zoom tonight",
            current_runtime_lines=[
                "Runtime Note: example-annotation (kind=custom).",
            ],
        )
        user_msg = str(messages[-1]["content"])

        assert "Runtime Note: example-annotation" in user_msg
        assert "kind=custom" in user_msg

    def test_consecutive_same_role_merged(self, tmp_path):
        builder = _builder(tmp_path)
        history = [{"role": "user", "content": "previous user message"}]
        messages = builder.build_messages(history, "new message")
        assert len(messages) == 2  # system + merged user
        assert "previous user message" in str(messages[1]["content"])
        assert "new message" in str(messages[1]["content"])

    def test_different_role_appended(self, tmp_path):
        builder = _builder(tmp_path)
        history = [{"role": "assistant", "content": "previous response"}]
        messages = builder.build_messages(history, "new message")
        assert len(messages) == 3  # system + assistant + user

    def test_media_with_history(self, tmp_path):
        png = tmp_path / "img.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        history = [{"role": "assistant", "content": "see this"}]
        messages = builder.build_messages(history, "check image", media=[str(png)])
        user_msg = messages[-1]["content"]
        assert isinstance(user_msg, list)
        assert any(b.get("type") == "image_url" for b in user_msg)
