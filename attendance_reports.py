"""Daily attendance subsets shared by the viewer and all three exports."""
from datetime import datetime

from report_tables import export_docx, export_pdf, export_xlsx


REPORT_TYPES = ['Full Attendance', 'Early Attendance', 'Checked In Only', 'Absentees']
REPORT_NOTES = {
    'Early Attendance': 'Employees who checked in before their allocated configured shift start. Shift allocation uses the nearest configured shift start.',
    'Checked In Only': 'Employees with a recorded check-in, including those whose checkout is still pending.',
    'Absentees': 'Roster employees with no recorded check-in on each listed date. Today’s absences are provisional until all shifts have started.',
}


def subset_tables(days, departments, mode, shift_start_for, employee_id_for):
    """Filter after attendance/absence calculation; never infer absence from early-only rows."""
    if mode not in REPORT_NOTES:
        raise ValueError('Choose an attendance subset report.')
    columns = ['Date', 'Department', 'Employee Name', 'ID', 'Status']
    if mode != 'Absentees':
        columns += ['Check In', 'Check Out', 'Hours Worked']
    if mode == 'Early Attendance':
        columns += ['Shift Start', 'Minutes Early']
    tables = []
    for day, attendance in days:
        records = []
        for department_key, data in attendance.items():
            department = departments[department_key]
            if mode == 'Absentees':
                for name in data['absent']:
                    records.append({'Date': day.isoformat(), 'Department': department['label'],
                                    'Employee Name': name, 'ID': employee_id_for(department_key, name),
                                    'Status': 'Absent'})
                continue
            for row in data['present']:
                check_in = row.get('check_in')
                if not check_in or check_in == '-':
                    continue
                record = {'Date': day.isoformat(), 'Department': department['label'],
                          'Employee Name': row['name'], 'ID': str(row['employee_id']),
                          'Status': 'Present', 'Check In': check_in,
                          'Check Out': '' if row.get('check_out', '-') == '-' else row['check_out'],
                          'Hours Worked': '' if row.get('hours', '-') == '-' else row['hours']}
                if mode == 'Early Attendance':
                    arrival = datetime.fromisoformat(day.isoformat() + 'T' + check_in)
                    start = shift_start_for(department_key, arrival)
                    minutes = int((start - arrival).total_seconds() // 60)
                    if minutes <= 0:
                        continue
                    record.update({'Status': 'Early', 'Shift Start': start.strftime('%Y-%m-%d %H:%M'),
                                   'Minutes Early': minutes})
                records.append(record)
        records.sort(key=lambda r: (r['Department'], r.get('Check In', ''), r['Employee Name'], r['ID']))
        tables.append((day.strftime('%A, %d %B %Y'), columns, records))
    return tables


def export_subset(mode, site, start_date, end_date, tables, logo_path=None):
    title = f'{site} — {mode}'
    subtitle = f'{start_date:%d %b %Y} to {end_date:%d %b %Y}'
    context = [REPORT_NOTES[mode], 'Times are in Tanzania local time (UTC+03:00).']
    options = {'logo_path': logo_path, 'context_lines': context}
    return {
        'docx': export_docx(title, subtitle, tables, **options),
        'pdf': export_pdf(title, subtitle, tables, **options),
        'xlsx': export_xlsx(title, subtitle, tables, sheet_name=mode, **options),
    }


def render_subset_report(mode, site, start_date, end_date, tables, tag, logo_path=None):
    import pandas as pd
    import streamlit as st
    st.markdown('### ' + site + ' — ' + mode)
    st.info(REPORT_NOTES[mode])
    records = [record for _, _, rows in tables for record in rows]
    days, count, staff = st.columns(3)
    days.metric('Days', (end_date - start_date).days + 1)
    count.metric('Matching Daily Records', len(records))
    staff.metric('Unique Staff', len({(record['ID'], record['Employee Name']) for record in records}))
    downloads, viewer = st.tabs(['Download Reports', 'View Reports'])
    with viewer:
        for heading, columns, rows in tables:
            st.markdown('#### ' + heading)
            if rows:
                st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True, use_container_width=True)
            else:
                st.info('No matching employees for this date.')
    with downloads:
        st.caption('Downloads contain the same filtered employees and dates as View Reports.')
        files = export_subset(mode, site, start_date, end_date, tables, logo_path)
        formats = [('Word (.docx)', 'docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                   ('PDF', 'pdf', 'application/pdf'),
                   ('Excel (.xlsx)', 'xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')]
        for column, (label, extension, mime) in zip(st.columns(3), formats):
            with column:
                st.download_button('Download ' + label, data=files[extension],
                    file_name=f"{site.replace(' ', '_')}_{mode.replace(' ', '_')}_{tag}.{extension}",
                    mime=mime, on_click='ignore')
