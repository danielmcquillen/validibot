def set_org(client, user, org):
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()
