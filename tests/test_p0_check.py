from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("chat_type", "text", "expected"),
    [
        ("private", "@fedocc привет", ("true", "direct_mention", "mention")),
        ("group", "@fedocc привет", ("true", "direct_mention", "mention")),
        (
            "channel",
            "Опубликовано распределение студентов",
            ("false", "channel_digest_only", "none"),
        ),
        ("channel", "@fedocc посмотри", ("true", "direct_mention", "mention")),
    ],
)
def test_p0_check_prints_only_safe_fields(
    monkeypatch,
    capsys,
    chat_type,
    text,
    expected,
) -> None:
    import app.cli.p0_check as cli

    settings = Settings(
        _env_file=None,
        p0_mention_usernames="fedocc,me,fedornikonov",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    cli.main(["--chat-type", chat_type, "--text", text])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        f"is_p0={expected[0]}",
        f"reason_category={expected[1]}",
        f"matched_signal={expected[2]}",
        f"chat_type={chat_type}",
        "is_outgoing=false",
    ]
    assert text not in captured.out


def test_p0_check_outgoing_message_is_never_p0(monkeypatch, capsys) -> None:
    import app.cli.p0_check as cli

    settings = Settings(
        _env_file=None,
        p0_mention_usernames="fedocc,me,fedornikonov",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    cli.main(
        [
            "--chat-type",
            "private",
            "--text",
            "@fedocc привет",
            "--outgoing",
            "true",
        ]
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "is_p0=false",
        "reason_category=self_message",
        "matched_signal=self_message",
        "chat_type=private",
        "is_outgoing=true",
    ]


@pytest.mark.parametrize(
    ("text", "is_p0", "matched_signal"),
    [
        ("завтра в 10 самолет ты придешь?", "true", "private_time_sensitive"),
        ("завтра в 10 самолёт ты придёшь?", "true", "private_time_sensitive"),
        ("сегодня пойдешь гулять?", "true", "private_planning"),
        ("сегодня пойдёшь гулять?", "true", "private_planning"),
        ("ало ответь", "true", "private_ping_reply"),
        ("алло ответь", "true", "private_ping_reply"),
        ("привет как дела?", "false", "none"),
        ("как дела?", "false", "none"),
    ],
)
def test_p0_check_private_recall_signals(
    monkeypatch,
    capsys,
    text,
    is_p0,
    matched_signal,
) -> None:
    import app.cli.p0_check as cli

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))

    cli.main(["--chat-type", "private", "--text", text, "--outgoing", "false"])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        f"is_p0={is_p0}",
        f"reason_category={'private_signal' if is_p0 == 'true' else 'none'}",
        f"matched_signal={matched_signal}",
        "chat_type=private",
        "is_outgoing=false",
    ]
    assert text not in captured.out
