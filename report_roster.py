"""Build reports from current terminal users, never from historical scans alone."""
from copy import deepcopy

from roster_store import normalized, configured_names


def current_report_roster(site, users):
    """Return a report-only copy; absent current users remain on the roster.

    Never persist missing users as deletions: a report is a current snapshot.
    A fresh terminal search also prevents a lost local SQLite override from
    restoring someone whose account was already removed from the terminal.
    """
    current = deepcopy(site)
    for department in current['employees'].values():
        if department.get('visitor_only'):
            continue
        ids = {normalized(name): str(employee_id)
               for name, employee_id in department.get('member_ids', {}).items()}
        members, member_ids = [], {}
        for name in department.get('members', []):
            name_key = normalized(name)
            name_keys = configured_names(department, name)
            employee_id = ids.get(name_key)
            matches = [user for user in users
                       if normalized(user.get('name', '')) in name_keys
                       and (employee_id is None or str(user.get('employeeNo', '')) == employee_id)]
            # An ID-less static name must identify exactly one current user.
            if len(matches) != 1:
                continue
            live_id = str(matches[0].get('employeeNo') or '').strip()
            if not live_id:
                continue
            members.append(name)
            member_ids[name] = live_id
        department['members'] = members
        department['member_ids'] = member_ids
    return current


def current_report_events(events, site, mobile_manager_name=None):
    """Filter before name-based department matching and every report/export.

    Use the current, assigned terminal ID and canonical roster name. A reused
    ID must not import a former employee's named scans. Mohamed's journey uses
    different terminal IDs and is retained only where he is still assigned
    or explicitly configured as a visitor.
    """
    members_by_id = {}
    names_by_id = {}
    allowed_mobile_name = None
    for department in site['employees'].values():
        ids = department.get('member_ids', {})
        for name in department.get('members', []):
            if mobile_manager_name and normalized(name) == normalized(mobile_manager_name):
                allowed_mobile_name = name
            if not department.get('visitor_only') and name in ids:
                members_by_id[str(ids[name])] = name
                names_by_id[str(ids[name])] = configured_names(department, name)
    filtered = []
    for event in events:
        employee_id = str(event.get('employeeNoString') or event.get('employeeNo') or '').strip()
        name = members_by_id.get(employee_id)
        if name is not None and event.get('name') and normalized(event['name']) not in names_by_id[employee_id]:
            name = None
        if allowed_mobile_name and normalized(event.get('name', '')) == normalized(allowed_mobile_name):
            name = allowed_mobile_name
        if name is not None:
            filtered.append({**event, 'name': name})
    return filtered
