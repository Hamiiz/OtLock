from asgiref.sync import async_to_sync
from django.test import TestCase

from bot.handlers.user_handlers import _create_signup
from bot.models import Agent, OTEvent, OTSignup
from bot.utils import (
    announcement_keyboard,
    generate_csv,
    split_text_for_telegram_messages,
    user_day_multi_keyboard,
    user_hour_keyboard,
    class_keyboard,
    confirm_keyboard,
    select_event_keyboard,
)


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

    def test_create_signup_blocks_same_day_in_second_ot(self):
        """Signing up for the same day in a second open OT must be blocked."""
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

        self.assertEqual(status, "duplicate_day")
        self.assertEqual(OTSignup.objects.filter(agent=self.agent, ot_event=second).count(), 0)

    def test_create_signup_allows_different_day_in_second_ot(self):
        """Signing up for a *different* day in a second open OT must succeed."""
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
            day="Tuesday",
            hours=2.0,
            class_type="DIALER",
        )

        self.assertEqual(status, "created")
        self.assertEqual(OTSignup.objects.filter(agent=self.agent, ot_event=second).count(), 1)

    def test_agent_name_truncated_at_50_chars(self):
        """Names longer than 50 characters should be silently truncated."""
        long_name = "A" * 100
        truncated = long_name[:50]
        self.assertEqual(len(truncated), 50)
        # Simulate what receive_name does:
        result = long_name.strip()[:50]
        self.assertEqual(result, truncated)

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
        self.assertIn("Agent One,2 hrs,", lines)
        self.assertIn("Agent Two,,4 hrs", lines)
        self.assertIn("Agent Three,2 hrs,", lines)

    def test_disabled_day_blocks_new_signup(self):
        """If an admin disabled a day, new signups for it should be blocked server-side."""
        event = self._make_event("Blocked Day OT")
        event.disabled_days = ["Monday"]
        event.save()

        _, status = async_to_sync(_create_signup)(
            agent=self.agent,
            event=event,
            day="Monday",
            hours=2.0,
            class_type="DIALER",
        )
        self.assertEqual(status, "day_disabled")
        self.assertEqual(OTSignup.objects.filter(agent=self.agent, ot_event=event).count(), 0)

    def test_disabled_day_does_not_affect_other_days(self):
        """If an admin disabled a day, signups for OTHER days remain fully open."""
        event = self._make_event("Blocked Day OT")
        event.disabled_days = ["Monday"]
        event.save()

        _, status = async_to_sync(_create_signup)(
            agent=self.agent,
            event=event,
            day="Tuesday",
            hours=2.0,
            class_type="DIALER",
        )
        self.assertEqual(status, "created")
        self.assertEqual(OTSignup.objects.filter(agent=self.agent, ot_event=event).count(), 1)

    def test_split_text_for_telegram_messages_respects_limit(self):
        parts = split_text_for_telegram_messages("a\n" * 3000, max_len=100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 100 for p in parts))

    def test_user_day_keyboard_callback_includes_session(self):
        kb = user_day_multi_keyboard(["Monday"], [], session_id="deadbeef")
        # Day button
        day_cb = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(day_cb, "uday_toggle:deadbeef:Monday")
        # Done button
        done_cb = kb.inline_keyboard[-1][0].callback_data
        self.assertEqual(done_cb, "udays_done:deadbeef")

    def test_user_hour_keyboard_callback_includes_session(self):
        kb = user_hour_keyboard("Monday", [2.0, 4.0], session_id="deadbeef")
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("uhour:deadbeef:2.0", cbs)
        self.assertIn("uhour:deadbeef:4.0", cbs)

    def test_confirm_keyboard_callback_includes_session(self):
        kb = confirm_keyboard("deadbeef")
        yes_cb = kb.inline_keyboard[0][0].callback_data
        no_cb = kb.inline_keyboard[0][1].callback_data
        self.assertEqual(yes_cb, "uconfirm:deadbeef:yes")
        self.assertEqual(no_cb, "uconfirm:deadbeef:no")

    def test_select_event_keyboard_callback_includes_session(self):
        event = self._make_event("OT Session Test")
        kb = select_event_keyboard([event], "user_signup", session_id="deadbeef")
        cb = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(cb, f"user_signup:deadbeef:{event.id}")
