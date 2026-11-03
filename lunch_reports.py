"""Optional Buguruni day report: arrival, lunch out/back, and departure."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from roster_store import normalized

LOCAL_TIMEZONE = timezone(timedelta(hours=3))
SCHEDULE = 'Day shift: 08:00-16:00 | Lunch: 13:00-14:00'
SLOTS = [('check_in', 'Check In'), ('lunch_out', 'Lunch Out'),
         ('lunch_in', 'Lunch In'), ('check_out', 'Check Out')]
DAILY_COLUMNS = ['Status', 'Employee Name', 'ID', 'Check In', 'Lunch Out',
                 'Lunch In', 'Check Out', 'Lunch (min)', 'Net Hours', 'Missing Clockings']
SUMMARY_COLUMNS = ['Employee Name', 'ID', 'Present', 'Absent', 'Complete', 'Incomplete',
                   'Avg In', 'Avg Lunch Out', 'Avg Lunch In', 'Avg Out', 'Net Hours']


def duration_text(minutes):
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


def parse_lunch_events(events, start_date, end_date):
    """Classify scans by time, so a missing scan never shifts the other slots.

    The first scan before 13:00 is arrival; the first in [13:00,14:00)
    is lunch out; the first in [14:00,16:00) is lunch back; the first at
    or after 16:00 is departure. Times are actual scans, never defaults.
    This optional report uses calendar days, not overnight shift allocation.
    """
    grouped = defaultdict(list)
    for event in events:
        employee_id = str(event.get('employeeNoString') or event.get('employeeNo') or '').strip()
        name = str(event.get('name') or '').strip()
        if not employee_id or not name:
            continue
        try:
            timestamp = datetime.fromisoformat(str(event.get('time', '')).replace('Z', '+00:00'))
        except ValueError:
            continue
        timestamp = timestamp.replace(tzinfo=LOCAL_TIMEZONE) if timestamp.tzinfo is None else timestamp.astimezone(LOCAL_TIMEZONE)
        timestamp = timestamp.replace(second=0, microsecond=0)
        if start_date <= timestamp.date() <= end_date:
            grouped[(employee_id, normalized(name), timestamp.date())].append((timestamp, name))

    rows = []
    for (employee_id, _, day), scans in grouped.items():
        scans.sort()
        selected = {}
        for timestamp, name in scans:
            minute = timestamp.hour * 60 + timestamp.minute
            slot = ('check_in' if minute < 13 * 60 else 'lunch_out' if minute < 14 * 60
                    else 'lunch_in' if minute < 16 * 60 else 'check_out')
            selected.setdefault(slot, timestamp)
        missing = [label for key, label in SLOTS if key not in selected]
        complete = not missing
        lunch_minutes = None
        if 'lunch_out' in selected and 'lunch_in' in selected:
            lunch_minutes = int((selected['lunch_in'] - selected['lunch_out']).total_seconds() // 60)
        net_minutes = None
        if complete:
            net_minutes = int(((selected['lunch_out'] - selected['check_in'])
                               + (selected['check_out'] - selected['lunch_in'])).total_seconds() // 60)
        rows.append({'employee_id': employee_id, 'name': scans[0][1], 'date': day.isoformat(),
                     **{key: selected[key].strftime('%H:%M') if key in selected else '' for key, _ in SLOTS},
                     'complete': complete, 'missing': ', '.join(missing),
                     'lunch_minutes': lunch_minutes, 'net_minutes': net_minutes,
                     'net_hours': duration_text(net_minutes) if net_minutes is not None else ''})
    return sorted(rows, key=lambda row: (row['date'], normalized(row['name']), row['employee_id']))


def average_time(rows, key):
    minutes = [int(row[key][:2]) * 60 + int(row[key][3:5]) for row in rows if row.get(key)]
    if not minutes:
        return ''
    mean = sum(minutes) // len(minutes)
    return f'{mean // 60:02d}:{mean % 60:02d}'


def lunch_tables(rows, site, start_date, end_date):
    """Use the verified current roster for both absent and scanned staff."""
    by_employee = defaultdict(list)
    for row in rows:
        by_employee[(row['employee_id'], normalized(row['name']))].append(row)
    order = list(site['dept_order'])
    # The home manager is shown only when he has a scan, as in normal reports.
    for key, department in site['employees'].items():
        if key not in order and not department.get('visitor_only') and any(
                normalized(member) == normalized(row['name'])
                for member in department.get('members', []) for row in rows):
            order.append(key)
    total_days = (end_date - start_date).days + 1
    daily, summary = [], []
    members_by_dept = {}
    for key in order:
        department = site['employees'][key]
        if department.get('visitor_only'):
            continue
        members = []
        for name in department.get('members', []):
            employee_id = str(department.get('member_ids', {}).get(name, ''))
            records = by_employee.get((employee_id, normalized(name)), [])
            members.append((name, employee_id, records))
        if members:
            members_by_dept[key] = members
            summary_rows = []
            for name, employee_id, records in members:
                complete = sum(record['complete'] for record in records)
                total = sum(record['net_minutes'] for record in records if record['net_minutes'] is not None)
                summary_rows.append({'Employee Name': name, 'ID': employee_id,
                                     'Present': len(records), 'Absent': total_days - len(records),
                                     'Complete': complete, 'Incomplete': len(records) - complete,
                                     'Avg In': average_time(records, 'check_in'),
                                     'Avg Lunch Out': average_time(records, 'lunch_out'),
                                     'Avg Lunch In': average_time(records, 'lunch_in'),
                                     'Avg Out': average_time(records, 'check_out'),
                                     'Net Hours': duration_text(total) if complete else ''})
            summary.append((department['label'], SUMMARY_COLUMNS, summary_rows))
    day = start_date
    while day <= end_date:
        for key, members in members_by_dept.items():
            daily_rows = []
            for name, employee_id, records in members:
                record = next((row for row in records if row['date'] == day.isoformat()), None)
                daily_rows.append({'Status': 'Complete' if record and record['complete'] else 'Incomplete' if record else 'Absent',
                                   'Employee Name': name, 'ID': employee_id,
                                   **{label: record[key] if record else '' for key, label in SLOTS},
                                   'Lunch (min)': record['lunch_minutes'] if record and record['lunch_minutes'] is not None else '',
                                   'Net Hours': record['net_hours'] if record else '',
                                   'Missing Clockings': record['missing'] if record else ''})
            daily.append((f"{day.strftime('%A, %d %B %Y')} — {site['employees'][key]['label']}", DAILY_COLUMNS, daily_rows))
        day += timedelta(days=1)
    return daily, summary


def export_lunch_docx(title, subtitle, tables, logo_path=None, *, context_lines=None):
    from report_tables import export_docx
    context = context_lines if context_lines is not None else [SCHEDULE, 'Blank clockings mean no scan was recorded. Net hours require all four clockings.']
    return export_docx(title, subtitle, tables, logo_path, context_lines=context)


def export_lunch_pdf(title, subtitle, tables, logo_path=None, *, context_lines=None):
    from report_tables import export_pdf
    context = context_lines if context_lines is not None else [SCHEDULE, 'Blank clockings mean no scan was recorded. Net hours require all four clockings.']
    return export_pdf(title, subtitle, tables, logo_path, context_lines=context)


def export_lunch_xlsx(title, subtitle, tables, logo_path=None, *, context_lines=None, sheet_name='Lunch report'):
    from report_tables import export_xlsx
    context = context_lines if context_lines is not None else [SCHEDULE, 'Blank clockings mean no scan was recorded. Net hours require all four clockings.']
    return export_xlsx(title, subtitle, tables, logo_path, context_lines=context, sheet_name=sheet_name)


def render_lunch_report(events, site, start_date, end_date, tag, logo_path=None):
    import pandas as pd
    import streamlit as st
    rows = parse_lunch_events(events, start_date, end_date)
    daily, summary = lunch_tables(rows, site, start_date, end_date)
    st.markdown('### Buguruni report with lunch time')
    st.caption(SCHEDULE)
    st.info('Clock four times: arrival at 8 am, lunch out at 1 pm, lunch back at 2 pm, and departure at 4 pm. '
            'Missing clockings stay blank. Net hours are calculated only when all four clockings are present.')
    days = (end_date - start_date).days + 1
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('Days', days)
    m2.metric('Staff Roster', sum(len(table[2]) for table in summary))
    m3.metric('Complete Days', sum(row['complete'] for row in rows))
    m4.metric('Incomplete Days', sum(not row['complete'] for row in rows))
    downloads, viewer = st.tabs(['Download Reports', 'View Reports'])
    with viewer:
        daily_view, summary_view = st.tabs(['Daily Attendance with Lunch', 'Period Summary with Lunch'])
        for target, tables in [(daily_view, daily), (summary_view, summary)]:
            with target:
                for heading, columns, records in tables:
                    st.markdown(f'**{heading}**')
                    st.dataframe(pd.DataFrame(records, columns=columns), hide_index=True, use_container_width=True)
    subtitle = f"Site: Buguruni | {start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}"
    with downloads:
        daily_tab, summary_tab = st.tabs(['Daily with Lunch', 'Summary with Lunch'])
        for target, label, tables in [(daily_tab, 'Daily', daily), (summary_tab, 'Summary', summary)]:
            with target:
                columns = st.columns(3)
                exporters = [('Word (.docx)', 'docx', export_lunch_docx,
                              'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                             ('PDF', 'pdf', export_lunch_pdf, 'application/pdf'),
                             ('Excel (.xlsx)', 'xlsx', export_lunch_xlsx,
                              'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')]
                for column, (name, extension, exporter, mime) in zip(columns, exporters):
                    data = exporter(f'BUGURUNI {label.upper()} REPORT WITH LUNCH TIME', subtitle, tables, logo_path)
                    with column:
                        st.download_button(f'Download {name}', data=data,
                                           file_name=f'Buguruni_{label}_With_Lunch_{tag}.{extension}', mime=mime)
