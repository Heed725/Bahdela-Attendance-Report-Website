"""Authorized terminal operations and the small, optional management panel."""
import os
import copy
import sqlite3
from datetime import date, time
from pathlib import Path

import streamlit as st

from admin_auth import active_account, login, logout, require_permission
from hikvision import HikvisionClient, HikvisionError, response_message
from registration_sites import load_sites
from roster_store import RosterStore, normalized, configured_names
from report_roster import current_report_roster


def secrets_dict():
    try:
        return st.secrets.to_dict()
    except Exception:
        return {}


def roster_store():
    config = secrets_dict()
    path = config.get('ROSTER_DB_PATH') or os.environ.get('ROSTER_DB_PATH')
    return RosterStore(path or Path(__file__).parent / 'data' / 'employee_roster.sqlite3')


def client_for(site_name, sites):
    secret_values = secrets_dict()
    configured = load_sites(secret_values, os.environ)[site_name]
    fallback = sites[site_name]
    # Reuse the report app's existing connection when no per-site override is set.
    password = configured.password or fallback['password']
    return HikvisionClient(configured.url, configured.username, password,
                           configured.timeout, configured.verify_tls)


def report_site_for(site_name, sites, store):
    # Re-read saved changes so another session's deletions are respected too.
    current = copy.deepcopy(sites)
    store.apply(current)
    users = client_for(site_name, current).search_users()
    return current_report_roster(current[site_name], users)


def verify_identity(client, employee):
    live = client.get_user(employee['employeeNo'])
    if not live or normalized(live.get('name', '')) != normalized(employee.get('name', '')):
        raise HikvisionError('This employee changed on the device. Refresh the user list and select again.')
    return live


def add_employee(state, client, store, record, site, department, job=None):
    actor = require_permission(state, 'add')
    client.create_user(record)
    store.save(actor, 'add', record['employee_no'], record['name'], destination=site, department=department, job=job)


def delete_employee(state, client, store, employee, site, sites=None):
    actor = require_permission(state, 'delete')
    member = require_report_member(employee, site, sites, store)
    verify_identity(client, employee)
    client.delete_user(employee['employeeNo'])
    store.save(actor, 'delete', employee['employeeNo'], employee['name'], source=site,
               old_name=member['roster_name'])


def delete_employees(state, client, store, employees, site, sites):
    """Delete only the checked people; retain failures for review or retry."""
    require_permission(state, 'delete')
    deleted, failed, seen = [], [], set()
    for employee in employees:
        employee_id = str(employee['employeeNo'])
        if employee_id in seen:
            continue
        seen.add(employee_id)
        try:
            delete_employee(state, client, store, employee, site, sites=sites)
            deleted.append(employee)
        except HikvisionError as exc:
            failed.append({'employee': employee, 'error': str(exc)})
        except (OSError, sqlite3.Error):
            failed.append({'employee': employee, 'error': 'The terminal may have changed, but its roster update could not be saved. Refresh the device list and check the data directory.'})
            # Stop if storage fails; do not continue changing other terminals.
            break
    return {'deleted': deleted, 'failed': failed}


def prepare_transfer(state, source_client, target_client, store, employee, source, target, department, sites=None, job=None):
    actor = require_permission(state, 'transfer')
    require_report_member(employee, source, sites, store)
    if source == target or source_client.base_url == target_client.base_url:
        raise HikvisionError('Choose a different destination location and terminal.')
    live = verify_identity(source_client, employee)
    # Preserve access rights and validity, but never copy passwords, cards or biometrics.
    if target_client.get_user(live['employeeNo']):
        raise HikvisionError('That employee ID already exists at the destination. No records were changed.')
    allowed = ('employeeNo', 'name', 'userType', 'gender', 'Valid', 'doorRight', 'RightPlan')
    user = {key: live[key] for key in allowed if key in live}
    result = target_client._request('PUT', '/ISAPI/AccessControl/UserInfo/Record?format=json', json={'UserInfo': user})
    if not result.ok:
        raise HikvisionError('Destination employee was not created: ' + response_message(result))
    verify_identity(target_client, employee)
    store.save(actor, 'prepare transfer', employee['employeeNo'], employee['name'], destination=target, department=department, job=job)
    return {'employee': dict(employee), 'source': source, 'target': target, 'department': department, 'job': job}


def finish_transfer(state, source_client, target_client, store, pending, sites=None):
    actor = require_permission(state, 'transfer')
    employee = pending['employee']
    member = require_report_member(employee, pending['source'], sites, store)
    if source_client.base_url == target_client.base_url:
        raise HikvisionError('Source and destination resolve to the same terminal. Check site configuration.')
    verify_identity(target_client, employee)
    verify_identity(source_client, employee)
    source_client.delete_user(employee['employeeNo'])
    store.save(actor, 'transfer', employee['employeeNo'], employee['name'],
               source=pending['source'], destination=pending['target'], department=pending['department'],
               job=pending.get('job'), old_name=member['roster_name'])


def render_login():
    account = active_account(st.session_state)
    if not account:
        with st.popover('Login', use_container_width=True):
            with st.form('login_form'):
                username = st.text_input('Username', key='login_username')
                password = st.text_input('Password', type='password', key='login_password')
                submitted = st.form_submit_button('Log in', use_container_width=True)
            if submitted:
                ok, message = login(st.session_state, username, password,
                                    secrets_dict().get('admin_accounts', {}))
                if ok:
                    st.session_state.pop('login_password', None)
                    st.session_state['management_open'] = True
                    st.rerun()
                st.error(message)
    else:
        with st.popover(account, use_container_width=True):
            st.caption('Employee management')
            if st.button('Manage employees', use_container_width=True):
                st.session_state['management_open'] = not st.session_state.get('management_open', False)
                st.rerun()
            if st.button('Log out', use_container_width=True):
                logout(st.session_state)
                st.rerun()


def report_employees(site, sites):
    """Use the report's saved assignment roster, including absent employees."""
    result = []
    for department, config in sites[site]['employees'].items():
        # Visiting staff are assigned to another location, not this site's roster.
        if config.get('visitor_only'):
            continue
        for name in config.get('members', []):
            ids = {normalized(n): str(i) for n, i in config.get('member_ids', {}).items()}
            jobs = {normalized(n): j for n, j in config.get('member_jobs', {}).items()}
            result.append({'name': name, 'employeeNo': ids.get(normalized(name), ''),
                           'name_keys': configured_names(config, name),
                           'department': department, 'job': jobs.get(normalized(name), config.get('default_job', config.get('label', department)))})
    return result


def filter_report_users(users, site, sites):
    roster = report_employees(site, sites)
    matches = []
    for user in users:
        # A configured ID must match. ID-less names may use only site-specific,
        # explicit aliases. Never infer an identity from shared first names.
        candidates = [r for r in roster if normalized(user.get('name', '')) in r['name_keys']
                      and (not r['employeeNo'] or r['employeeNo'] == str(user.get('employeeNo', '')))]
        if len(candidates) != 1:
            continue
        member = candidates[0]
        if not member['employeeNo'] and sum(
                normalized(u.get('name', '')) in member['name_keys'] for u in users) != 1:
            continue
        matches.append({**user, 'department': member['department'], 'job': member['job'],
                        'roster_name': member['name']})
    return sorted(matches, key=lambda r: (normalized(r['name']), str(r.get('employeeNo', ''))))


def require_report_member(employee, site, sites, store):
    if sites is None:
        raise HikvisionError('The site report roster is required before managing this employee.')
    current = copy.deepcopy(sites)
    store.apply(current)
    matches = filter_report_users([employee], site, current)
    if not matches:
        raise HikvisionError('This employee is not in this location’s report roster. Refresh the list.')
    return matches[0]


def department_choice(site, sites, key, store):
    departments = [name for name, dept in sites[site]['employees'].items() if not dept.get('visitor_only')]
    choice = st.selectbox('Department *', departments, index=None, placeholder='Where should this person work?', key=key)
    with st.popover('Add a new department at ' + site):
        with st.form(key + '_new_form'):
            name = st.text_input('New department name *', max_chars=80, key=key + '_new_name')
            job = st.text_input('Default job title *', max_chars=80, key=key + '_new_job')
            c1, c2 = st.columns(2)
            start = c1.time_input('Shift starts', time(8), key=key + '_start')
            end = c2.time_input('Shift ends', time(17), key=key + '_end')
            color = st.color_picker('Department colour', '#8B0000', key=key + '_color')
            submitted = st.form_submit_button('Create department', key=key + '_create')
        if submitted:
            actor = require_permission(st.session_state, 'add')
            if any(normalized(existing) == normalized(name) for existing in sites[site]['employees']):
                st.error('This department already exists at ' + site)
            else:
                store.create_department(actor, site, name, job,
                    'Shift: ' + start.strftime('%H:%M') + ' - ' + end.strftime('%H:%M'), color)
                st.session_state['management_message'] = 'Department added at ' + site + '. Select it to place the employee.'
                st.rerun()
    return choice


def job_choice(site, department, sites, key):
    config = sites[site]['employees'][department]
    jobs = list(dict.fromkeys([config.get('default_job', config.get('label', department))]
                             + config.get('jobs', []) + list(config.get('member_jobs', {}).values())))
    if not jobs:
        jobs = [config.get('label', department)]
    choice = st.selectbox('Job title *', jobs + ['Add a new job title…'], index=None, key=key,
                          placeholder='Choose a job for this department')
    if choice == 'Add a new job title…':
        return ' '.join(st.text_input('New job title *', max_chars=80, key=key + '_new').split())
    return choice


def load_report_users(site, sites, key, clear_ticks=False):
    if st.button('Load / refresh report employees', key=key + '_refresh'):
        require_permission(st.session_state, 'list')
        st.session_state[key + '_users'] = client_for(site, sites).search_users()
        if clear_ticks:
            for widget_key in list(st.session_state):
                if widget_key.startswith(key + '_tick_'):
                    st.session_state.pop(widget_key, None)
    loaded = st.session_state.get(key + '_users')
    if loaded is None:
        st.info('Load employees assigned to this site’s report roster first.')
        return []
    users = filter_report_users(loaded, site, sites)
    if not users:
        st.info('No matching report-roster employees were found on this terminal.')
    return users


def selected_employee(site, sites, key):
    users = load_report_users(site, sites, key)
    if not users:
        return None
    choice = st.selectbox('Employee', [str(u['employeeNo']) for u in users], index=None,
        format_func=lambda employee_id: next(f"{u['employeeNo']} · {u['name']} · {u['department']} · {u['job']}"
                                             for u in users if str(u['employeeNo']) == employee_id), key=key + '_choice')
    return next((u for u in users if str(u['employeeNo']) == choice), None)


def render_delete_list(site, sites, store):
    key = 'manage_delete_' + site
    results = st.session_state.get(key + '_results')
    if results:
        if results['deleted']:
            st.success('Deleted from ' + site + ': ' + ', '.join(
                f"{u['name']} (ID {u['employeeNo']})" for u in results['deleted']))
        for failure in results['failed']:
            employee = failure['employee']
            st.error(f"{employee['name']} (ID {employee['employeeNo']}): {failure['error']}")
    users = load_report_users(site, sites, key, clear_ticks=True)
    if not users:
        return
    st.write('Tick the employees who should be removed from this location.')
    selected = []
    for employee in users:
        if st.checkbox(f"{employee['name']} · ID {employee['employeeNo']} · {employee['department']} · {employee['job']}",
                       key=key + '_tick_' + str(employee['employeeNo'])):
            selected.append(employee)
    st.caption(f'{len(selected)} of {len(users)} employees selected')
    if selected:
        st.warning(f"Delete {len(selected)} selected employee(s) from {site}? This permanently removes their terminal access, linked credentials and report roster placement at this location.")
    if st.button('Delete selected employees', key=key + '_submit', disabled=not selected):
        with st.spinner('Deleting the selected employees…'):
            results = delete_employees(st.session_state, client_for(site, sites), store, selected, site, sites)
        st.session_state[key + '_results'] = results
        deleted_ids = {str(employee['employeeNo']) for employee in results['deleted']}
        st.session_state[key + '_users'] = [u for u in st.session_state.get(key + '_users', [])
                                           if str(u.get('employeeNo')) not in deleted_ids]
        st.rerun()


def enroll_employee(employee, site, sites, key):
    st.caption(f"Biometrics: {employee['name']} · {employee['employeeNo']} · {site}")
    photo = st.file_uploader('Face photograph (JPEG)', type=['jpg', 'jpeg'], key=key + '_photo')
    if st.button('Save face at this location', disabled=photo is None, key=key + '_face'):
        require_permission(st.session_state, 'enroll')
        client = client_for(site, sites)
        verify_identity(client, employee)
        result = client.upload_face(employee['employeeNo'], photo.getvalue(), photo.name)
        if not result.ok:
            raise HikvisionError('Face was not saved: ' + response_message(result))
        st.success('Face saved at ' + site)
    st.caption('For fingerprint capture, the employee must stand beside this location’s terminal.')
    if st.button('Capture and save fingerprint', key=key + '_finger'):
        require_permission(st.session_state, 'enroll')
        client = client_for(site, sites)
        verify_identity(client, employee)
        captured = client.capture_fingerprint(1)
        if not captured.data:
            raise HikvisionError(captured.message or 'No fingerprint returned by the terminal.')
        result = client.apply_fingerprint(employee['employeeNo'], captured.data, 1)
        if not result.ok:
            raise HikvisionError('Fingerprint was not saved: ' + response_message(result))
        st.success('Fingerprint saved at ' + site)


def render_management(sites, store):
    if not active_account(st.session_state) or not st.session_state.get('management_open'):
        return
    with st.expander('Employee management', expanded=True):
        st.caption('Add, delete or transfer employees on the selected Hikvision terminal and in the report roster.')
        try:
            _render_management(sites, store)
        except (HikvisionError, PermissionError, ValueError) as exc:
            st.error(str(exc))
        except (OSError, sqlite3.Error):
            st.error('Could not save the roster. The terminal may already have changed. Refresh the device list and check the data directory before continuing.')


def _render_management(sites, store):
    add_tab, transfer_tab, delete_tab, history_tab = st.tabs(['Add employee', 'Transfer employee', 'Delete employee', 'History'])
    names = list(sites)
    with add_tab:
        site = st.selectbox('Where should this person be placed? *', names, index=None, key='manage_add_site')
        if site:
            department = department_choice(site, sites, 'manage_add_department_' + site, store)
            job = job_choice(site, department, sites, 'manage_add_job_' + site + '_' + department) if department else None
            with st.form('manage_add_form_' + site):
                employee_id = st.text_input('Employee ID *', max_chars=32)
                full_name = st.text_input('Full name *', max_chars=128)
                gender = st.selectbox('Gender', ['unspecified', 'male', 'female'])
                c1, c2 = st.columns(2)
                start = c1.date_input('Valid from', date.today())
                end = c2.date_input('Valid until', date(2036, 12, 31))
                plan = st.number_input('Access plan template', min_value=1, max_value=255, value=1)
                confirm = st.checkbox('I confirm this person should be added to this location and department.')
                submitted = st.form_submit_button('Add employee')
            if submitted:
                if not department or not job or not full_name.strip() or not employee_id.strip().isalnum() or end < start or not confirm:
                    st.error('Enter a valid ID and name, choose a department and job title, check the dates and confirm the placement.')
                else:
                    record = {'employee_no': employee_id.strip(), 'name': ' '.join(full_name.split()),
                              'gender': gender, 'valid_from': start.isoformat(), 'valid_until': end.isoformat(), 'access_plan': int(plan)}
                    add_employee(st.session_state, client_for(site, sites), store, record, site, department, job=job)
                    st.session_state['management_enroll'] = {'site': site, 'employee': {'employeeNo': record['employee_no'], 'name': record['name']}}
                    store.apply(sites)
                    st.success('Employee added to ' + site + ' / ' + department + ' / ' + job)
            saved = st.session_state.get('management_enroll')
            if saved and saved['site'] == site:
                enroll_employee(saved['employee'], site, sites, 'manage_add_enroll_' + site)
    with transfer_tab:
        pending = st.session_state.get('management_transfer')
        if pending:
            st.info(f"{pending['employee']['name']}: destination record saved at {pending['target']}. Source remains active at {pending['source']} until you finish.")
            st.warning('Face, fingerprint and card credentials are not copied automatically. Enroll at the destination or arrange enrollment before removing the source record.')
            enroll_employee(pending['employee'], pending['target'], sites, 'manage_transfer_enroll')
            confirmed = st.checkbox('The destination is ready, or enrollment has been arranged. Remove this employee and their credentials from the source.', key='manage_transfer_confirm')
            if st.button('Finish transfer', disabled=not confirmed):
                finish_transfer(st.session_state, client_for(pending['source'], sites), client_for(pending['target'], sites), store, pending, sites=sites)
                st.session_state.pop('management_transfer', None)
                st.session_state['management_message'] = 'Transfer completed. Employee removed from the source and placed at the destination.'
                st.rerun()
            if st.button('Leave both locations active for now'):
                st.session_state.pop('management_transfer', None)
                st.session_state['management_message'] = 'Both records remain active. Use Delete employee at the source when ready.'
                st.rerun()
        else:
            source = st.selectbox('Current location *', names, index=None, key='manage_transfer_source')
            if source:
                employee = selected_employee(source, sites, 'manage_transfer_' + source)
                target = st.selectbox('Where should this person be transferred? *', [n for n in names if n != source], index=None, key='manage_transfer_target_' + source)
                department = department_choice(target, sites, 'manage_transfer_department_' + target, store) if target else None
                job = job_choice(target, department, sites, 'manage_transfer_job_' + target + '_' + department) if department else None
                confirmed = st.checkbox('I confirm the selected employee and destination.', key='manage_transfer_start_confirm_' + source)
                if st.button('Prepare destination', disabled=not (employee and target and department and job and confirmed)):
                    st.session_state['management_transfer'] = prepare_transfer(st.session_state, client_for(source, sites), client_for(target, sites), store, employee, source, target, department, sites=sites, job=job)
                    st.rerun()
    with delete_tab:
        site = st.selectbox('Delete from which location? *', names, index=None, key='manage_delete_site')
        if site:
            render_delete_list(site, sites, store)
    with history_tab:
        st.dataframe(store.history(), use_container_width=True, hide_index=True)
