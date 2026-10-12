"""Persist roster overrides and an audit trail outside the tracked source."""
import json
import sqlite3
from pathlib import Path


def normalized(name):
    return ' '.join(str(name).split()).casefold()


class RosterStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS changes (site TEXT, name_key TEXT, record TEXT, PRIMARY KEY(site,name_key))')
            db.execute('CREATE TABLE IF NOT EXISTS departments (site TEXT, name_key TEXT, name TEXT, config TEXT, PRIMARY KEY(site,name_key))')
            db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT CURRENT_TIMESTAMP, actor TEXT, action TEXT, employee_id TEXT, name TEXT, source TEXT, destination TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def save(self, actor, action, employee_id, name, source=None, destination=None, department=None, old_name=None, job=None):
        # Both sides of a transfer are committed in one database transaction.
        with self.connect() as db:
            if source:
                self._put(db, source, old_name or name, {'deleted': True, 'employee_id': employee_id})
            if destination:
                self._put(db, destination, name, {'name': name, 'employee_id': employee_id, 'department': department, 'job': job})
            db.execute('INSERT INTO audit(actor,action,employee_id,name,source,destination) VALUES (?,?,?,?,?,?)',
                       (actor, action, employee_id, name, source, destination))

    @staticmethod
    def _put(db, site, name, record):
        db.execute('INSERT OR REPLACE INTO changes VALUES (?,?,?)', (site, normalized(name), json.dumps(record)))

    def create_department(self, actor, site, name, job, shift, color):
        name, job = ' '.join(name.split()), ' '.join(job.split())
        if not name or not job or len(name) > 80 or len(job) > 80:
            raise ValueError('Enter a department name and default job title (up to 80 characters).')
        config = {'label': name.upper(), 'shift': shift, 'header_hex': color.lstrip('#'),
                  'members': [], 'jobs': [job], 'default_job': job}
        with self.connect() as db:
            try:
                db.execute('INSERT INTO departments VALUES (?,?,?,?)',
                           (site, normalized(name), name, json.dumps(config)))
            except sqlite3.IntegrityError as exc:
                raise ValueError('This department already exists at the selected site.') from exc
            db.execute('INSERT INTO audit(actor,action,name,destination) VALUES (?,?,?,?)',
                       (actor, 'add department', name, site))

    def apply(self, sites):
        with self.connect() as db:
            departments = db.execute('SELECT site,name,config FROM departments').fetchall()
            rows = db.execute('SELECT site,name_key,record FROM changes').fetchall()
        for site_name, name, encoded in departments:
            if site_name not in sites:
                continue
            site = sites[site_name]
            site['employees'].setdefault(name, json.loads(encoded))
            site.setdefault('dept_colors', {})[name] = '#' + site['employees'][name]['header_hex']
            if name not in site['dept_order']:
                site['dept_order'].append(name)
        for site_name, name_key, encoded in rows:
            if site_name not in sites:
                continue
            site = sites[site_name]
            record = json.loads(encoded)
            for dept in site['employees'].values():
                dept['members'] = [m for m in dept.get('members', []) if normalized(m) != name_key]
                dept['member_jobs'] = {n: j for n, j in dept.get('member_jobs', {}).items() if normalized(n) != name_key}
                dept['member_ids'] = {n: i for n, i in dept.get('member_ids', {}).items() if normalized(n) != name_key}
            if not record.get('deleted'):
                department = record['department']
                if department not in site['employees']:
                    continue
                dept = site['employees'][department]
                dept.setdefault('members', []).append(record['name'])
                dept.setdefault('member_ids', {})[record['name']] = record['employee_id']
                if record.get('job'):
                    dept.setdefault('member_jobs', {})[record['name']] = record['job']
                    jobs = dept.setdefault('jobs', [])
                    if record['job'] not in jobs:
                        jobs.append(record['job'])
                if department not in site['dept_order']:
                    site['dept_order'].append(department)

    def history(self):
        with self.connect() as db:
            cursor = db.execute('SELECT timestamp,actor,action,employee_id,name,source,destination FROM audit ORDER BY id DESC LIMIT 100')
            keys = [column[0] for column in cursor.description]
            return [dict(zip(keys, row)) for row in cursor.fetchall()]
