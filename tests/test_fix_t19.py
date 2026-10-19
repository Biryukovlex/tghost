"""Copy fidelity, explicit history restore, and no inferred draft workflow."""
from app.studio.markdown import render_markdown_html






def test_clipboard_html_uses_br_line_breaks():
    html = render_markdown_html("**Title**\nSecond line\n> quote")
    assert html == "<b>Title</b><br>Second line<br><blockquote>quote</blockquote>"
    assert "\n" not in html


def test_links_with_balanced_parentheses_render_whole():
    from app.studio.markdown import render_markdown_plain

    body = "See [w](https://en.wikipedia.org/wiki/Foo_(bar)) and [j](javascript:alert(1)) end"
    html = render_markdown_html(body)
    assert '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">w</a>' in html
    assert "javascript" not in html.lower() and "j end" in html
    assert render_markdown_plain(body) == "See w (https://en.wikipedia.org/wiki/Foo_(bar)) and j end"


import pytest as _pytest


@_pytest.mark.asyncio
async def test_restore_alias_moves_the_pointer_and_never_mints_a_version():
    from app.studio.repository import MemoryStudioRepository

    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    draft = await repo.create_draft(conversation_id=conversation["id"], channel_id=1, payload={"body": "v1 body", "creative": True})
    v2 = await repo.update_draft(draft_id=draft["id"], payload={"body": "v2 body"}, expected_revision=draft["revision"], origin="user_edit")
    assert v2["current_version"] == 2
    restored = await repo.restore_draft_version(draft_id=draft["id"], version=1, expected_revision=v2["revision"])
    assert restored["body"] == "v1 body" and restored["current_version"] == 1
    again = await repo.restore_draft_version(draft_id=draft["id"], version=1, expected_revision=restored["revision"])
    assert again["current_version"] == 1 and again["revision"] == restored["revision"]
    assert len(await repo.list_draft_versions(draft_id=draft["id"])) == 2




@_pytest.mark.asyncio
async def test_change_request_is_not_rewritten_into_a_server_tool_contract(monkeypatch):
    """The language model chooses the explicit chat-scoped draft operation."""
    from app.studio import service as service_module
    from app.studio.repository import MemoryStudioRepository
    from app.studio.service import StudioService
    from app.config import Settings

    captured: dict = {}

    class _Adapter:
        build_run_input = staticmethod(service_module.AGUIAdapter.build_run_input)

        def __init__(self, **kwargs):
            captured["agent"] = kwargs.get("agent")
            captured["run_input"] = kwargs["run_input"]

        async def run_stream(self, **kwargs):
            captured["deps"] = kwargs.get("deps")
            if False:
                yield None

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr(service_module, "AGUIAdapter", _Adapter)
    repo = MemoryStudioRepository()
    conversation = await repo.create_conversation(channel_id=1)
    await repo.create_draft(conversation_id=conversation["id"], channel_id=1, payload={"body": "Existing post body", "creative": True})
    settings = Settings(studio_test_mode=True, api_id=1, api_hash="h", session_string="s", channels="@t", admin_password="p", session_secret="s", data_dir="/tmp")
    service = StudioService(repo, settings)
    import json, uuid
    async def contract_for(message: str):
        captured.clear()
        body = json.dumps({"threadId": str(conversation["id"]), "runId": str(uuid.uuid4()), "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": message}], "tools": [], "context": [], "forwardedProps": {}}).encode()
        response = await service.stream_request(None, body)
        async for _ in response.body_iterator:
            pass
        deps = captured.get("deps")
        assert deps is not None, "adapter was not invoked"
        return captured["run_input"].messages[0].content

    # Draft words and edit requests no longer rewrite the tool contract.
    assert "Add bold title and subtitles in the draft" == await contract_for("Add bold title and subtitles in the draft")
    for message in ("Make it shorter and drop the emoji", "Сделай короче и убери эмодзи", "Add a subtitle before the second paragraph", "Убери подпись в конце"):
        tools = await contract_for(message)
        assert tools == message, (message, tools)
    # Without a change request nothing is forced.
    assert "thanks, looks good" == await contract_for("thanks, looks good")
