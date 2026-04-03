"""
Management command: python manage.py seed_test_data

Creates local OT testing data with 10 agents and multiple OT events.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from bot.models import Agent, OTEvent, OTSignup


class Command(BaseCommand):
    help = "Seed local OT test data with 10 agents and multiple scenarios."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing OT events/signups/agents before seeding.",
        )

    def handle(self, *args, **options):
        reset = options.get("reset", False)

        if reset:
            OTSignup.objects.all().delete()
            OTEvent.objects.all().delete()
            Agent.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing OT test data cleared."))

        # Create 10 agents
        agents = []
        for idx in range(1, 11):
            agent, _ = Agent.objects.update_or_create(
                telegram_id=900000 + idx,
                defaults={
                    "telegram_username": f"test_agent_{idx}",
                    "agent_name": f"Test Agent {idx:02d}",
                },
            )
            agents.append(agent)

        now = timezone.now()

        # Open OT #1 (Shift 1)
        shift1 = OTEvent.objects.create(
            title="Shift 1 OT - Mon/Tue",
            created_by_telegram_id=1,
            days=["Monday", "Tuesday"],
            time_slots={"Monday": [2.0, 4.0], "Tuesday": [2.0, 4.0]},
            max_agents=10,
            is_open=True,
            deadline=now + timedelta(hours=24),
            group_chat_id=settings.GROUP_CHAT_ID,
        )

        # Open OT #2 (Shift 2) - same days to test overlap
        shift2 = OTEvent.objects.create(
            title="Shift 2 OT - Mon/Tue",
            created_by_telegram_id=1,
            days=["Monday", "Tuesday"],
            time_slots={"Monday": [2.0, 4.0], "Tuesday": [2.0, 4.0]},
            max_agents=10,
            is_open=True,
            deadline=now + timedelta(hours=30),
            group_chat_id=settings.GROUP_CHAT_ID,
        )

        # Closed OT for history/export testing
        weekend_closed = OTEvent.objects.create(
            title="Weekend OT (Closed Sample)",
            created_by_telegram_id=1,
            days=["Saturday", "Sunday"],
            time_slots={"Saturday": [8.0, 10.0], "Sunday": [8.0, 10.0]},
            max_agents=None,
            is_open=False,
            deadline=now - timedelta(hours=2),
            group_chat_id=settings.GROUP_CHAT_ID,
        )

        # Assign first 5 agents to open Shift 1
        class_cycle = ["TOPLIST", "IB", "DIALER", "IB", "TOPLIST"]
        for idx, agent in enumerate(agents[:5]):
            OTSignup.objects.get_or_create(
                agent=agent,
                ot_event=shift1,
                day="Monday",
                defaults={"hours": 2.0 if idx % 2 == 0 else 4.0, "class_type": class_cycle[idx]},
            )
            OTSignup.objects.get_or_create(
                agent=agent,
                ot_event=shift1,
                day="Tuesday",
                defaults={"hours": 2.0, "class_type": class_cycle[idx]},
            )

        # Assign next 5 agents to open Shift 2
        class_cycle2 = ["DIALER", "IB", "TOPLIST", "DIALER", "IB"]
        for idx, agent in enumerate(agents[5:]):
            OTSignup.objects.get_or_create(
                agent=agent,
                ot_event=shift2,
                day="Monday",
                defaults={"hours": 4.0 if idx % 2 == 0 else 2.0, "class_type": class_cycle2[idx]},
            )
            OTSignup.objects.get_or_create(
                agent=agent,
                ot_event=shift2,
                day="Tuesday",
                defaults={"hours": 2.0, "class_type": class_cycle2[idx]},
            )

        # Put a few signups in closed OT history
        for agent in agents[:3]:
            OTSignup.objects.get_or_create(
                agent=agent,
                ot_event=weekend_closed,
                day="Saturday",
                defaults={"hours": 8.0, "class_type": "IB"},
            )

        self.stdout.write(self.style.SUCCESS("Seeded OT test data successfully."))
        self.stdout.write(
            f"- Agents: {Agent.objects.count()}\n"
            f"- Open OTs: {OTEvent.objects.filter(is_open=True).count()}\n"
            f"- Closed OTs: {OTEvent.objects.filter(is_open=False).count()}\n"
            f"- Signups: {OTSignup.objects.count()}"
        )
