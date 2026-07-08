from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import SlackConfig
from modules.im.base import MessageContext
from modules.im.slack import SlackBot, _SLACK_MARKDOWN_TEXT_LIMIT


class SlackMarkdownFooterTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_result_folds_subtext_into_body(self):
        """A 12k-30k inline Slack result falls back to the legacy mrkdwn path,
        which can't carry a context-footer block; the show_duration footnote must
        be folded onto the body instead of silently dropped (Codex P2)."""
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        slack._ensure_clients = lambda: None
        captured = {}

        async def fake_send(context, text, parse_mode=None, reply_to=None):
            captured["text"] = text
            return "ts-1"

        slack.send_message = fake_send
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        big = "x" * (_SLACK_MARKDOWN_TEXT_LIMIT + 1)

        message_id = await slack.send_markdown_message(context, big, subtext="✅ ⏱️ 5s · 🪙 1.2k tok")

        self.assertEqual(message_id, "ts-1")
        self.assertEqual(captured["text"], f"{big}\n\n✅ ⏱️ 5s · 🪙 1.2k tok")

    async def test_long_result_without_subtext_unchanged(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        slack._ensure_clients = lambda: None
        captured = {}

        async def fake_send(context, text, parse_mode=None, reply_to=None):
            captured["text"] = text
            return "ts-2"

        slack.send_message = fake_send
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        big = "y" * (_SLACK_MARKDOWN_TEXT_LIMIT + 1)

        await slack.send_markdown_message(context, big)

        self.assertEqual(captured["text"], big)


if __name__ == "__main__":
    unittest.main()
