# Bahdela Attendance Report Website

The existing seven-site attendance report website, with an optional **Login** button at the top right. Reports, colours, departments, exports and the existing mobile-manager reporting remain available as before.

## Buguruni lunch report

Buguruni has two report buttons on the same row. **View / Generate Reports** opens the existing shift report. **View report with lunch time**, on its right, opens a separate day report with four actual scans: **08:00 arrival → 13:00 lunch out → 14:00 lunch back → 16:00 departure**. The extra button is hidden at all other sites.

The lunch report uses Buguruni's current verified employee roster and the selected date range, and reads only the Buguruni terminal's scans. It uses a calendar-day 08:00-16:00 schedule for this mode, rather than each department's normal/overnight shift. The normal report keeps its existing shift and cross-site manager movement rules.

For each employee and day, the first scan before 13:00 is arrival, the first from 13:00 up to 14:00 is lunch out, the first from 14:00 up to 16:00 is lunch back, and the first from 16:00 onward is departure. Extra scans in a time period do not fill a different period. Missing clockings stay blank and are listed in **Missing Clockings**; current staff with no scans appear as absent. No scheduled time is inserted as a clocking. Net hours require all four scans and equal morning work plus afternoon work. Thus 08:00/13:00/14:00/16:00 gives **7h 00m**, excluding the one-hour lunch break.

The screen report and Word, PDF and Excel downloads include daily lunch details and a period summary. The summary shows complete/incomplete days and totals net hours only for complete days. Lunch downloads include `_With_Lunch_` in their filenames so they are easy to distinguish from normal reports.

## Employee management

The requested **Kanza**, **HR** and **admin** accounts all have permission to add, delete, transfer and enroll employees. Passwords are verified on the server against PBKDF2 hashes. Login expires after 30 minutes of inactivity. Log out on shared computers. The management panel is hidden until login.

- **Add employee:** choose “Where should this person be placed?”, then a department and a job title. Jobs are scoped to that department; choose **Add a new job title…** if needed. Enter the employee ID, full name, gender, validity dates and access plan. Confirm and click **Add employee**. An existing terminal ID is rejected. Add a face photograph or capture a fingerprint afterward if needed.
- **Transfer employee:** choose the current location and **Load / refresh report employees**. Select the employee by ID and name, choose their destination location, department and job, then **Prepare destination**. The destination record is created and checked first; the source stays active. Enroll the person at the destination, then confirm and **Finish transfer**. Only this final step removes the source record and its credentials. If interrupted, both records remain active; load the source in **Delete employee** to complete removal once the destination is ready.
- **Delete employee:** choose the location, click **Load / refresh report employees**, then tick the people to remove in the checkbox list. Check the selected count and click **Delete selected employees**. Multiple employees can be removed in one action; unchecked employees are untouched. Each person is verified and deleted individually. Successful deletions and failures are shown separately, and failed employees remain in the list for review or retry. This deletes their terminal access and linked credentials at that location and removes the roster placement there. Other locations and historical scan events are unchanged.
- **History:** see the last 100 roster operations, with the operator, employee ID and locations.

Delete and transfer selectors show only employees assigned to the chosen site’s **report roster**, including absent staff, who match a terminal record. Unlisted terminal accounts and visiting staff assigned elsewhere are excluded. Known employee IDs must match; names must match a full roster name or an explicitly configured alias at that site. No fuzzy first-name matching is used. The roster is checked again before deletion or transfer.

Upanga includes these explicit terminal-name mappings: **Athumani Y Shebly** → Athumani Yusuph Sheby (Oil); **Hamlat S ghindu** → Hamlati Shaibu Ghindu (Oil); **Mnyeto M Rashid** → Mnyeto Miraji Rashidi (**Security, ID 1365**). Mnyeto appears once in Security; his former duplicate Oil entry is removed. The management list shows the terminal's name and ID. Reports use the existing roster name. Deletion and completed transfer remove the original source roster entry as well as the terminal account, including when the selected terminal name is an alias. Ambiguous matches are excluded until an employee ID is configured.

Every **View / Generate Reports** action now reads the selected terminal's complete current employee list. Staff removed from that terminal are excluded from the report roster, old scan events, totals, daily and period summaries, and Word/PDF/Excel downloads. Current registered staff with no scans still appear as absent. Employee IDs are checked before scans can be matched to a department; sharing the first two names cannot bring a deleted person back into a report. The existing visiting-manager journey across locations is preserved. If the current employee list cannot be verified, generation stops with an error so an incomplete roster is not presented as a valid report.

To create a department, select a site under **Add employee**, open **Add a new department at [site]**, enter its name, default job title, shift start/end and report colour, then click **Create department**. Select the new department and job before adding the employee. It is saved for that site only, appears as a normal report section and can also be selected as a transfer destination. Assigned job titles appear in the report’s existing Role column; original staff roles are retained.

Face images, fingerprint templates and cards are **not copied during transfer**. A JPEG may be uploaded at the destination and fingerprint slot 1 may be captured there, matching the registration app. The employee must stand beside the destination terminal for fingerprint capture. Biometric files are sent to the terminal, not written to the roster database. Choose “Leave both locations active for now” if enrollment must happen later.

Device failures stop the workflow and are displayed. A rejected destination or duplicate ID never triggers source deletion. Removal is confirmed by checking the terminal again. The app only sends a single explicitly selected employee ID to the delete endpoint; it never requests deletion of all users.

## Locations and connections

Locations match [Bahdela Hikvision Registration](https://github.com/Heed725/Bahdela_Hikvision_Registration_Streamlit): Buguruni, Puma Upanga, Puma Ocean Road, Puma Survey, India, Kibaha and Livingstone. The registration repository itself is unchanged.

Management uses the same default terminal addresses as that app and can use the same `[sites."Location"]` Streamlit Secrets configuration: `url`, `username`, `password`, `timeout`, `verify_tls`. Environment overrides use names such as `SITE_PUMA_UPANGA_PASSWORD`. When no device password override is configured, the report website's existing connection credentials are reused. Logged-in management does not need the public registration enrollment PIN.

## Saved roster and hosting

Roster additions, removals, transfers and history are saved in SQLite at `data/employee_roster.sqlite3`. They survive page refreshes and process restarts **when this directory is retained**. They are applied after the original roster initialization on every run, so the Python roster lists do not need manual editing.

For durable hosting, set `ROSTER_DB_PATH` in Streamlit Secrets or the environment to a SQLite file on a **persistent server volume**, for example `/var/lib/bahdela/employee_roster.sqlite3`, and back up that file. The process must have write permission. On multiple replicas, use one shared supported persistent database deployment rather than independent local files.

**Streamlit Community Cloud's local disk is not durable across replacement/redeployment.** The default local database is therefore insufficient for permanent cloud roster storage. Deploy on a server with a persistent volume before relying on saved roster changes. Device records themselves are stored on the Hikvision terminals. This update does not provision hosting storage or change your deployment settings.

The current-terminal check prevents a deleted terminal account from reappearing in newly generated reports even if a local deletion override is lost on redeploy. It does not recover lost custom departments, job assignments, additions or audit history. Reports use current registration; historical scans are retained on the device but removed employees are excluded even when an earlier report date is selected. Regenerate reports and download new copies after changing employees.

Optional `[admin_accounts]` secrets may override each account's password hash. Generate a hash without putting the password into a command argument:

```bash
python -c 'import getpass,secrets; from admin_auth import password_hash; print(password_hash(getpass.getpass("New password: "),secrets.token_hex(16)))'
```

Place the returned value under the account name in `[admin_accounts]`. Do not commit real secrets, biometric files or the database.

## Run and verify

```bash
pip install -r requirements.txt
streamlit run App.py
python -m unittest discover -s tests -v
```

Tests use mocked terminals and temporary databases; they never delete or register real people. Live terminal behavior depends on the model, firmware and network connectivity and must be checked with a supervised test employee. The new client follows Hikvision's UserInfo Search/Record and single-person UserInfoDetail Delete APIs.
