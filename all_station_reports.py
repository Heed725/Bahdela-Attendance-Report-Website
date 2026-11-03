"""Collect independent station snapshots, excluding failed or incomplete reads."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from report_roster import current_report_roster
from attendance_reports import REPORT_TYPES, REPORT_NOTES, render_subset_report

ALL_STATIONS = 'All stations'


def collect_station_reports(clients, sites, start_date, end_date):
    def load(item):
        name, client = item
        try:
            users = client.search_users()
            events = client.search_events(start_date, end_date)
            roster = current_report_roster(sites[name], users)
            # Connection credentials must never be retained in report snapshots.
            public_roster = {key: roster[key] for key in ('label', 'employees', 'dept_order', 'dept_colors')}
            fields = ('employeeNoString', 'employeeNo', 'name', 'time')
            attendance = [{key: event[key] for key in fields if key in event} for event in events]
            return name, {'roster': public_roster, 'events': attendance}, None
        except Exception as exc:
            return name, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(7, max(1, len(clients)))) as executor:
        results = list(executor.map(load, clients.items()))
    return ({name: result for name, result, error in results if error is None},
            {name: error for name, result, error in results if error is not None})


def combine_station_tables(station_tables):
    """Preserve dates, station names and IDs, including duplicate IDs across sites."""
    by_day = {}
    for name, tables in station_tables.items():
        for heading, columns, records in tables:
            if heading not in by_day:
                by_day[heading] = (['Station', *columns], [])
            by_day[heading][1].extend({'Station': name, **record} for record in records)
    return [(heading, columns, records) for heading, (columns, records) in by_day.items()]


def render_all_stations(sites, start_date, end_date, tag, generate, client_factory, tables_for, logo_path=None):
    import streamlit as st
    scope = (start_date.isoformat(), end_date.isoformat(),
             repr({name: config['employees'] for name, config in sites.items()}))
    if st.session_state.get('all_station_scope') != scope:
        st.session_state.pop('all_station_snapshot', None)
    if generate:
        st.session_state.pop('all_station_snapshot', None)
        clients, failures = {}, {}
        for name in sites:
            try:
                clients[name] = client_factory(name, sites)
            except Exception as exc:
                failures[name] = str(exc)
        with st.spinner('Checking stations and loading attendance…'):
            active, skipped = collect_station_reports(clients, sites, start_date, end_date)
        failures.update(skipped)
        st.session_state['all_station_scope'] = scope
        st.session_state['all_station_snapshot'] = {
            'active': active, 'skipped': failures,
            'generated_at': datetime.now(timezone(timedelta(hours=3))).strftime('%d %b %Y, %H:%M')}
    snapshot = st.session_state.get('all_station_snapshot')
    if snapshot is None:
        st.info('Generate reports to check all stations. Unavailable stations will be skipped automatically.')
        return
    active, skipped = snapshot['active'], snapshot['skipped']
    if skipped:
        st.warning('Skipped unavailable stations: ' + ', '.join(skipped))
        with st.expander('Station connection details'):
            for name, reason in skipped.items():
                st.write(name + ': ' + reason)
    if not active:
        st.error('No stations could be read. No attendance or absentee report was generated.')
        return
    st.success('Included active stations: ' + ', '.join(active))
    st.markdown('### Report Options')
    st.caption('Generated ' + snapshot['generated_at'] + ' (Tanzania time). Use View / Generate Reports to refresh attendance.')
    mode = st.selectbox('Attendance report type', REPORT_TYPES, key='attendance_report_type')
    if mode in REPORT_NOTES:
        st.caption(REPORT_NOTES[mode])
    tables = combine_station_tables(tables_for(active, mode))
    context = ['Included stations: ' + ', '.join(active)]
    if skipped:
        context.append('Skipped unavailable stations: ' + ', '.join(skipped))
    render_subset_report(mode, ALL_STATIONS, start_date, end_date, tables, tag, logo_path, context)
