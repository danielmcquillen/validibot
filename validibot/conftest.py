import pytest

from validibot.users.models import User
from validibot.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture(autouse=True)
def _ensure_billing_plans(db) -> None:
    """
    Ensure billing Plans exist for tests that create users/orgs.

    User creation triggers trial subscription setup which requires STARTER plan.
    """
    from validibot.billing.constants import PlanCode
    from validibot.billing.models import Plan

    Plan.objects.get_or_create(
        code=PlanCode.STARTER,
        defaults={
            "name": "Starter",
            "basic_launches_limit": 10000,
            "included_credits": 200,
            "max_seats": 2,
            "max_workflows": 10,
            "monthly_price_cents": 2900,
            "display_order": 1,
        },
    )
    Plan.objects.get_or_create(
        code=PlanCode.TEAM,
        defaults={
            "name": "Team",
            "basic_launches_limit": 100000,
            "included_credits": 1000,
            "max_seats": 10,
            "max_workflows": 100,
            "monthly_price_cents": 9900,
            "display_order": 2,
        },
    )
    Plan.objects.get_or_create(
        code=PlanCode.ENTERPRISE,
        defaults={
            "name": "Enterprise",
            "basic_launches_limit": 1000000,
            "included_credits": 5000,
            "max_seats": 100,
            "max_workflows": 1000,
            "monthly_price_cents": 0,
            "display_order": 3,
        },
    )


@pytest.fixture
def user(db) -> User:
    return UserFactory()
