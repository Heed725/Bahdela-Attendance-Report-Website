"""Read-only employee discovery, independent of report-roster membership."""
from concurrent.futures import ThreadPoolExecutor
from roster_store import normalized


def find_terminal_employees(clients, query):
    terms = [normalized(term) for term in query.split(',') if normalized(term)]
    if not terms:
        raise ValueError('Enter an employee name or ID.')

    def inspect(item):
        site, client = item
        try:
            users = client.search_users()
            matches = []
            for user in users:
                employee_id = str(user.get('employeeNo', ''))
                name = str(user.get('name', ''))
                if any(term == employee_id if term.isdigit() else term in normalized(name)
                       for term in terms):
                    valid = user.get('Valid') or {}
                    matches.append({
                        'Station': site, 'Employee ID': employee_id, 'Name': name,
                        'Fingerprints': user.get('numOfFP', 'Not reported'),
                        'Face photographs': user.get('numOfFace', 'Not reported'),
                        'Cards': user.get('numOfCard', 'Not reported'),
                        'User type': user.get('userType', 'Not reported'),
                        'Validity enabled': valid.get('enable', 'Not reported'),
                        'Valid from': valid.get('beginTime', 'Not reported'),
                        'Valid until': valid.get('endTime', 'Not reported'),
                        'Door access': user.get('doorRight', 'Not reported'),
                    })
            return matches, {'Station': site, 'Status': 'Checked', 'Records checked': len(users)}
        except Exception as exc:
            # A failed terminal must never be reported as having no matches.
            return [], {'Station': site, 'Status': 'Could not check: ' + str(exc), 'Records checked': 0}

    with ThreadPoolExecutor(max_workers=min(7, max(1, len(clients)))) as executor:
        results = list(executor.map(inspect, clients.items()))
    return ([match for matches, _ in results for match in matches],
            [status for _, status in results])
