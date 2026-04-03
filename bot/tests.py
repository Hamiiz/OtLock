from asgiref.sync import async_to_sync
from django.test import TestCase

from bot.handlers.user_handlers import _create_signup
from bot.models import Agent, OTEvent, OTSignup
from bot.utils import announcement_keyboard, generate_csv


class OTRegressionTests(TestCase):
    def setUp(self):
        self.agent = Agent.objects.create(
            telegram_id=1111,
            telegram_username="agent1",
            agent_name="Agent One",
        )

    def _make_event(self, title: str) -> OTEvent:
        return OTEvent.objects.create(
            title=title,
            created_by_telegram_id=9999,
            days=["Monday", "Tuesday"],
            time_slots={"Monday": [2.0, 4.0], "Tuesday": [2.0]},
            max_agents=None,
            is_open=True,
            group_chat_id=-1001234567890,
        )

    def test_announcement_keyboard_contains_event_specific_deeplink(self):
        kb = announcement_keyboard("MyBot", 42)
        self.assertIsNotNone(kb)
        url = kb.inline_keyboard[0][0].url
        self.assertEqual(url, "https://t.me/MyBot?start=signup_42")

    def test_create_signup_blocks_second_open_ot(self):
        first = self._make_event("OT A")
        second = self._make_event("OT B")
        OTSignup.objects.create(
            agent=self.agent,
            ot_event=first,
            day="Monday",
            hours=2.0,
            class_type="IB",
        )

        _, status = async_to_sync(_create_signup)(
            agent=self.agent,
            event=second,
            day="Monday",
            hours=4.0,
            class_type="DIALER",
        )

        self.assertEqual(status, "other_open_event")
        self.assertEqual(OTSignup.objects.filter(agent=self.agent, ot_event=second).count(), 0)

    def test_generate_csv_has_required_class_order_and_table_shape(self):
        event = self._make_event("OT CSV")
        agent2 = Agent.objects.create(telegram_id=2222, agent_name="Agent Two")
        agent3 = Agent.objects.create(telegram_id=3333, agent_name="Agent Three")

        OTSignup.objects.create(agent=self.agent, ot_event=event, day="Monday", hours=2.0, class_type="TOPLIST")
        OTSignup.objects.create(agent=agent2, ot_event=event, day="Tuesday", hours=4.0, class_type="IB")
        OTSignup.objects.create(agent=agent3, ot_event=event, day="Monday", hours=2.0, class_type="DIALER")

        csv_text = generate_csv(event, list(OTSignup.objects.filter(ot_event=event))).decode("utf-8")
        lines = csv_text.splitlines()

        toplist_idx = lines.index("Toplist")
        ib_idx = lines.index("IB")
        dialer_idx = lines.index("Dialer")
        self.assertTrue(toplist_idx < ib_idx < dialer_idx)

        self.assertIn("Agent Name,Monday,Tuesday", lines)
        self.assertIn("Agent One,2.0,", lines)
        self.assertIn("Agent Two,,4.0", lines)
        self.assertIn("Agent Three,2.0,", lines)
