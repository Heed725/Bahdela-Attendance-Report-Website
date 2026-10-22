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
- **Transfer employee:** choose the current location and **Load / refresh report employees**. Select the employee by ID and name, choose their destination location, department and job, then **Prepare destination**. The app exports their enrolled fingerprints, face photograph and cards, creates the destination employee, copies the credentials and reads them back to verify the copy. Review the copied counts, confirm and click **Finish transfer**. This rechecks the source and destination credentials, then removes the source employee and updates both report rosters. A partial copy keeps the source active and offers **Retry credential copy**. **Pause transfer (keep both active)** retains the pending workflow while the current browser session remains available; reopen **Manage employees** to continue.
- **Delete employee:** choose the location, click **Load / refresh report employees**, then tick the people to remove in the checkbox list. Check the selected count and click **Delete selected employees**. Multiple employees can be removed in one action; unchecked employees are untouched. Each person is verified and deleted individually. Successful deletions and failures are shown separately, and failed employees remain in the list for review or retry. This deletes their terminal access and linked credentials at that location and removes the roster placement there. Other locations and historical scan events are unchanged.
- **History:** see the last 100 roster operations, with the operator, employee ID and locations.

Delete and transfer selectors show only employees assigned to the chosen site’s **report roster**, including absent staff, who match a terminal record. Unlisted terminal accounts and visiting staff assigned elsewhere are excluded. Known employee IDs must match; names must match a full roster name or an explicitly configured alias at that site. No fuzzy first-name matching is used. The roster is checked again before deletion or transfer.

Upanga includes these explicit terminal-name mappings: **Athumani Y Shebly** → Athumani Yusuph Sheby (Oil); **Hamlat S ghindu** → Hamlati Shaibu Ghindu (Oil); **Mnyeto M Rashid** → Mnyeto Miraji Rashidi (**Security, ID 1365**). Mnyeto appears once in Security; his former duplicate Oil entry is removed. The management list shows the terminal's name and ID. Reports use the existing roster name. Deletion and completed transfer remove the original source roster entry as well as the terminal account, including when the selected terminal name is an alias. Ambiguous matches are excluded until an employee ID is configured.

Every **View / Generate Reports** action now reads the selected terminal's complete current employee list. Staff removed from that terminal are excluded from the report roster, old scan events, totals, daily and period summaries, and Word/PDF/Excel downloads. Current registered staff with no scans still appear as absent. Employee IDs are checked before scans can be matched to a department; sharing the first two names cannot bring a deleted person back into a report. The existing visiting-manager journey across locations is preserved. If the current employee list cannot be verified, generation stops with an error so an incomplete roster is not presented as a valid report.

To create a department, select a site under **Add employee**, open **Add a new department at [site]**, enter its name, default job title, shift start/end and report colour, then click **Create department**. Select the new department and job before adding the employee. It is saved for that site only, appears as a normal report section and can also be selected as a transfer destination. Assigned job titles appear in the report’s existing Role column; original staff roles are retained.

Transfers automatically copy all exported card numbers (including leading zeros), fingerprint slots and fingerprint types, plus the enrolled JPEG face photograph. A card already owned by another destination employee is rejected; the app never reassigns it. Card settings and fingerprint templates are compared after copying. Face content is checked with tolerance for JPEG recompression, and the destination image is checked again before source removal. Photographs, card numbers and templates stay in memory during requests; the pending session stores only identity, locations, counts and verification digests. The roster database contains placement/history, not credential data. No physical fingerprint capture is required for an automatic transfer.

**Copy enrolled face photograph** is enabled by default. An administrator may explicitly turn it off to transfer only fingerprints and cards when a source photograph cannot be exported. The choice is shown before preparation and completion and retained for retry and final verification. No face export, upload or comparison runs in that mode; any existing destination photograph is kept. Finishing still removes the source account and its linked credentials, so an excluded source photograph is not preserved there.

Automatic copying requires the devices' ISAPI export/import permissions and compatible firmware/fingerprint formats. This implementation targets standalone station terminals with destination fingerprint reader **1** and one face photograph per employee in face library **1**. It supports JSON and JSON/JPEG multipart face export, including device-hosted image URLs routed through the configured station address. If a terminal reports enrolled credentials but cannot export them, returns only a face model without a photograph, rejects an import or cannot confirm the saved data, the transfer stops with the source intact. It never silently skips enrolled credentials. If credentials change after preparation, use **Retry credential copy** before finishing. Test recognition on the destination terminal before confirming the final removal.

Pending transfers are kept in the current Streamlit session, not persisted across logout, a new browser session or deployment. If that session is lost after destination creation, both terminal records remain. Existing destination IDs are rejected by default. An administrator may explicitly select **Verify an existing destination account for this same employee without overwriting its credentials**. This permits reconciliation only when the exact employee ID and normalized full name, access settings, cards, fingerprints and photograph match the source. It performs no destination writes; differences stop the transfer with both accounts intact. The settings and credentials are checked again before source removal. Historical attendance scan events are not migrated or deleted.

Device failures stop the workflow and are displayed. A rejected destination or duplicate ID never triggers source deletion. Removal is confirmed by checking the terminal again. The app only sends a single explicitly selected employee ID to the delete endpoint; it never requests deletion of all users.

### Face and fingerprint enrollment

Release **2026.10.22.3** appears in the page footer and management panel so a stale cloud deployment can be identified. The app no longer starts a second server when already run by Streamlit. Select the station under **Add employee**, then click **Check terminal compatibility**; adding a person is not required. This performs only GET requests using that station's configured connection and displays the model, firmware and supported capture options without serial numbers, passwords or employee/biometric data. Fingerprint capture reads the device's advertised XML namespace/version before sending the condition. If both XML and JSON requests are rejected, the error distinguishes the two attempts. Photo uploads request connection closure to avoid reusing a socket closed by terminal firmware; network failures never trigger an automatic repeat write. Physical enrollment is not considered verified by the automated tests.

Fingerprint capture sends a namespaced XML `CaptureFingerPrintCond` request to `/ISAPI/AccessControl/CaptureFingerPrint`, and reads XML, JSON or multipart capture replies. Firmware that explicitly rejects that format is retried once using the JSON endpoint with the correct `CaptureFingerPrintCond` wrapper. Timeouts and busy responses are not retried automatically. The employee must place their finger on the selected station's scanner while capture is running. Fingerprint-reader failures inside HTTP 200 replies are treated as failures, and enrollment is only reported successful after reading the template back.

Face enrollment accepts readable JPEGs up to 20 MB with a minimum dimension of 80 pixels. It applies orientation, resizes to fit 640 × 640 and encodes within 200 KB. Uploads include the employee name, face-library/person identifiers and ordered metadata/image parts with explicit byte lengths. A firmware format rejection may retry the older `img` image-part name. The same upload handling is used during transfers. A successful upload is checked against the saved photograph before showing success. Error messages distinguish fingerprint capture, fingerprint saving and face saving; XML `ResponseStatus` errors are parsed rather than displayed as raw markup. These protocol checks do not replace a recognition test on the physical terminal.

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

Tests use mocked terminals and temporary databases; they never delete or register real people. Coverage includes multi-card/multi-fingerprint/face transfer, multipart and URL face export, partial failure and retry, card conflicts, count mismatches, changed credentials and blocked source deletion. Live terminal behavior depends on the model, firmware and network connectivity and must be checked with a supervised test employee. The client follows [Hikvision's documented ISAPI APIs](https://open.hikvision.com/hardware/v2/08%E5%8D%8F%E8%AE%AE%E9%80%8F%E4%BC%A0/%E6%98%8E%E7%9C%B8%E9%97%A8%E7%A6%81.html) for UserInfo Search/Record/Delete, CardInfo Search/Record, FingerPrintUpload, FingerPrint/SetUp and FDLib face search/upload.
