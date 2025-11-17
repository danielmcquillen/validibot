def set_org(client, user, org):
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()


def ensure_all_roles_exist():
    """
    Seed the Role table with one entry per RoleCode.
    Safe to call multiple times in tests.
    """
    from simplevalidations.users.constants import RoleCode
    from simplevalidations.users.models import Role

    for code in RoleCode.values:
        Role.objects.get_or_create(
            code=code,
            defaults={"name": getattr(RoleCode, code).label},
        )
