# HeatWave Blue Green Control Room

Flask console for OCI HeatWave blue-green creation, observation and switchover. The Compute instance principal authenticates OCI requests. SQL telemetry and scheduled-event administration use MySQL Connector/Python with passwords fetched from OCI Vault. This standalone application is maintained at [ivanxma/HW_BLUE_GREEN_CONTROL](https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL). The separate observer repository is independent.

Blue/Green roles and related-resource checks use the original source/target DB-system OCIDs and `active_db_system_id`, never display names. Endpoint handoff also verifies the saved Green SQL server UUID and hostname, plus the original Blue UUID and final GTIDs. A display-name swap does not change either role.

The creation form uses OCI-backed dropdowns for target shape, target MySQL version, target configuration and promoted HA configuration. Shape changes clear incompatible selections and reload configurations for that exact shape. Configuration choices show names and OCIDs; custom entries are limited to the selected compartment. Promoted HA configuration is optional when the target shape is unchanged and the original HA configuration can be reused; a different shape requires a matching promoted HA configuration. Preview and execution recheck the selections before any source event or HA mutation. Only available stable MySQL versions at or above the source version are offered; OCI validates deployment-specific compatibility on creation.

The **Enable HA** dialog uses a dropdown showing configuration name and OCID, with the current compatible configuration preselected. OCI ListConfigurations is scoped to the selected compartment and the DB-system shape. Only ACTIVE, shape-matching HA-compatible configurations are offered; custom configurations must inherit from a compatible default HA configuration and belong to the selected compartment. Shape and compatibility are checked again by the execution worker. No tenancy-wide configuration discovery is required.

HA enablement preserves an unchanged configuration by submitting only `isHighlyAvailable=true`. A different reviewed configuration is applied and awaited as a separate step before HA enablement; both changes are shown in confirmation. The same path is used for restored Blue HA, promoted Green HA and manual HA enablement. HA-only parameter rejections retain the non-sensitive OCI explanation and request ID. A completed native switchover with failed HA post-processing must not be rerun; use the promoted server's Enable HA action after reviewing current state.

When a database observation fails, live status facts, endpoint values, access mode, uptime, version/shape, GTIDs and SQL replication details show `*****`. No previous SQL values or OCI metadata fallback are presented as current live status. A successful recheck restores current values. OCI configuration remains separately identified in its own tab. Failed whole-detail requests also clear the previous status display.

The main menu has **Blue/Green Deployment**, **DB Systems**, and **Activity history**. The deployment menu contains environment selection, current execution, and Blue on the left / Green on the right. Each server has independent **Details**, **Replicas**, **Replication**, **Work Request**, **Variables**, **Status**, and **OCI Configuration** tabs using the same component as DB Systems. Tab selections, pagination and unchanged DOM nodes persist across polling. Deployment work requests include both members, the deployment, channels and journaled operations; unavailable replica or work-request discovery is informational and does not hide SQL telemetry.

The dashboard shows the DB-system list on the left and selected connection, HA, endpoints, read replicas, channels, GTIDs and work requests on the right. It defaults to every non-deleted state and provides DB-state, channel-state and read-replica filters. Blue/Green membership highlights server names with white text on blue/green backgrounds; normal status text remains green. DB systems, channels and replicas in DELETED state are hidden; replicas in any other state prevent creation. The workflow refreshes replica discovery before any mutation. The dashboard separates HA and standalone systems, excludes existing deployment members from creation, shows Blue and Green side by side, and refreshes every 15 seconds. Green indicates healthy/idle, orange indicates pending operations or unknown health, and red indicates errors. Both DB and environment detail panels read the last generated OCI channel-status result with its timestamp; unavailable status shows its reason. Polling never generates channel-status work requests. Workflow steps, work-request IDs, timestamps, durations, event choices and secret OCID references are persisted in the embedded MySQL `bluegreen` database. The journal is separate from the managed HeatWave databases.

## Install and sign in

Clone only this application:

```bash
git clone https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL.git bluegreen_control
cd bluegreen_control
```

Generated reports, validation evidence, local credentials, downloaded runtimes and database files are excluded from Git.

Deployment installs both embedded MySQL Community Server and embedded MySQL Shell under `.embedded/`. Setup discovers the latest available GA archives for the host platform and architecture from Oracle's [Server download page](https://dev.mysql.com/downloads/mysql/) and [Shell download page](https://dev.mysql.com/downloads/shell/), verifies the published checksums, and checks the installed binary versions. It does not depend on an OS-package MySQL server. Version discovery currently selects Server **26.7.0** and Shell **26.7.1**; it runs again when setup is rerun. A download or discovery failure stops setup rather than silently accepting an older version.

On a fresh installation, the login popup accepts:

- Username: `admin`
- Initial password: `ChangeMe123!`

The first step is mandatory password change. The dashboard and every operational API remain blocked until the password is changed. Choose a different password of at least 12 characters, then sign in again. Normal subsequent starts show the same login popup and accept the changed password. Setup reruns preserve existing passwords and do not reset the first-login flag.

MySQL authenticates the admin account through the application-owned Unix socket. The initialized `root@localhost` account is renamed to `admin@localhost`. Browser cookies contain only an opaque session ID. Login credentials and SQL connections remain in server-owned memory, expire after eight hours, and are cleared on logout or password rotation. The app checks the local MySQL connection before handling authenticated requests.

All DB-system registrations, Vault secret references, observation profiles, selected compartment preferences, event review records, original event states, workflow plans, work-request references and activity history reside in embedded MySQL. The application uses a separate socket-only `bluegreen_store` service account for background work. Its generated credential is stored in `.data/store.json` with mode `0600`; this is a local deployment credential, not a registration export. HeatWave replication/observation passwords remain in OCI Vault. No database passwords are placed in browser-visible sessions or runtime environment defaults.

## Local read-only OCI demo

```bash
cd bluegreen_control
./setup.sh --platform macos --mode none
BLUEGREEN_DEMO=1 PORT=5088 .venv/bin/python app.py
```

Open http://127.0.0.1:5088. The demo uses the same embedded MySQL admin login and mandatory password-change flow. Its OCI resources and telemetry are sample data, and cloud/remote SQL actions are disabled. For manual launches, start embedded MySQL first if it is stopped: `.venv/bin/python embedded_runtime.py start`. The HTTP/HTTPS start scripts do this automatically.

## Deploy on Compute

Use a VM with private access to both DB systems and HTTPS access to regional OCI APIs through a service gateway or NAT. Select the VM's compartment, placement, shape, boot volume, image, VCN/subnet and SSH public key for your environment. Allow TCP 3306 from the VM to managed DB systems and SSH from your administration CIDR. The local registration database has no TCP or X Protocol listener. A public IP is optional. The default OCI compartment comes from IMDSv2; `BLUEGREEN_COMPARTMENT_ID` is an explicit override.

Oracle Linux 9 minimal HTTPS deployment (`opc`, current user; port 443):

```bash
cd /home/opc/heatwave-bluegreen
./setup.sh --mode https
```

For validation against live discovery while blocking all real OCI mutations:

```bash
./setup.sh --mode https --read-only
```

The explicit form is equivalent:

Oracle Linux 9 (`opc`):

```bash
cd bluegreen_control
./setup.sh --platform ol9 --mode https --host 0.0.0.0 --port 443 --service-user opc
sudo systemctl status heatwave-bluegreen-local-mysql.service heatwave-bluegreen-https.service
```

Ubuntu 24.04 (`ubuntu`):

```bash
cd bluegreen_control
./setup.sh --platform ubuntu --mode https --host 0.0.0.0 --port 5443 --service-user ubuntu
sudo systemctl status heatwave-bluegreen-local-mysql.service heatwave-bluegreen-https.service
```

Setup installs prerequisites, downloads the current official tarballs, initializes the app-owned MySQL datadir, creates its schema, imports any old SQLite registrations once, and installs systemd services. MySQL uses `.data/mysql`, `.data/run/mysql.sock`, `.data/log/mysqld.err`, `.data/tmp` and the generated `etc/my.cnf`. The local MySQL service waits for socket readiness before the web service starts and survives web-service restarts. OL9 services launch through `/usr/bin/bash` under `unconfined_service_t` as the runtime user, so SELinux can remain enforcing when the application lives in that user’s home directory. The Shell executable is `.embedded/mysql-shell/current/bin/mysqlsh` (`./mysqlsh.sh` provides an app-local configuration directory) and is added to the web service's PATH. Platform hooks support OL8, OL9, Ubuntu and macOS; macOS is for local development. Ubuntu has app-local `libaio` compatibility and AppArmor hooks.

Replace the generated self-signed certificate with a trusted certificate, or specify `SSL_CERT_FILE` and `SSL_KEY_FILE` in the protected `.runtime.env`. Open the chosen HTTPS port in the OCI NSG/security list from your administrator CIDR. Sign in through the popup and change the initial admin password before using the console. `configure_admin.py` optionally changes an existing admin password locally and requires its current password; it never writes a password hash into `.runtime.env`.

Run exactly one application worker. A file lock prevents multiple processes sharing the database, and one executor serializes mutations. Do not use multiple workers, Flask reload, or multiple replicas. Long workflows continue after logout. Restart marks unfinished workflows `INTERRUPTED` and does not replay submitted OCI operations. Setup refuses to upgrade embedded MySQL while a workflow is running. Back up `.data/mysql` using a MySQL-aware backup before a server upgrade; retain the previous binary directory until the upgrade has been validated. Do not downgrade a datadir to an earlier MySQL version.

Default downloads are discovered from the vendor pages. For a pinned installation, supply `BLUEGREEN_MYSQL_SERVER_URL` and `BLUEGREEN_MYSQL_SERVER_SHA256`, or `BLUEGREEN_MYSQLSH_URL` and `BLUEGREEN_MYSQLSH_SHA256`, using an official HTTPS tarball URL. Optional version variables validate an explicitly selected package. An older installed server or Shell is not silently treated as current. Downloads and extracted binaries are ignored by Git.

For first boot, adapt `oci_compute_init.sh` (which defaults to this repository), and paste it in OCI Console → Create instance → Advanced options → Management → Initialization script. The application is at the repository root; `APP_SUBDIR` defaults to `.`. The user-requested default password is provisioned automatically; no additional plaintext bootstrap environment variable is needed. First web login must rotate it.

OL9 init variables (immediately below the shebang):

```bash
APP_REPO='https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL'
OS_FAMILY='ol9'
APP_USER='opc'
```

Ubuntu init variables:

```bash
APP_REPO='https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL'
OS_FAMILY='ubuntu'
APP_USER='ubuntu'
```

Install state is `/var/lib/heatwave-bluegreen-init`, and the log is `/var/log/heatwave-bluegreen-init.log`. The login banner reports installing, completed or failed. Check both systemd services and the init log after first boot. The old `.data/operations.sqlite3` is retained as a protected recovery copy after its transactional import; new registrations and updates use only MySQL.

## Instance-principal IAM: one compartment

Discovery and operations are confined to the selected compartment. No tenancy-wide MySQL grant is required. The compartment selector can show names because instance principals have built-in compartment inspection; that does not imply MySQL access. Changing to an inaccessible compartment clears the prior inventory and shows a warning, the failed operations and policy examples with that newly selected compartment OCID.

Create or reuse a dynamic group matching only this Compute instance, for example `ALL {instance.id = '<compute-instance-ocid>'}`. A discovery policy scoped to one compartment is:

```text
Allow dynamic-group <vm-dynamic-group> to read mysql-family in compartment id <selected-compartment-ocid>
```

If live execution is intended, grant management only in the operating compartment:

```text
Allow dynamic-group <vm-dynamic-group> to manage mysql-family in compartment id <selected-compartment-ocid>
Allow dynamic-group <vm-dynamic-group> to read secret-bundles in compartment id <secret-compartment-ocid>
Allow dynamic-group <vm-dynamic-group> to manage secrets in compartment id <secret-compartment-ocid>
Allow dynamic-group <vm-dynamic-group> to read vaults in compartment id <secret-compartment-ocid>
Allow dynamic-group <vm-dynamic-group> to use keys in compartment id <secret-compartment-ocid>
```

Attach the policy in the named compartment or an ancestor authorized to grant its permissions. Network attachment permissions must also be scoped to the chosen subnet/NSG compartment. A resource outside the selected compartment requires an explicit grant for that named compartment; the console does not expand permissions automatically. Missing access outside the selected compartment is informational and does not block operations inside it. Failed required discovery inside the selected compartment prevents creation until that compartment's permissions are resolved.

The former regional discovery scanned all visible compartments and could produce OCI 404 outside the VM's MySQL scope. It has been removed. Registrations moved to other compartments are outside this view; OCI validates source conflicts at creation. See [the specific VM IAM guide](docs/compartment-iam.html), [Oracle instance-principal documentation](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm), and [creation prerequisites](https://docs.oracle.com/en-us/iaas/mysql-database/doc/creating-blue-green-deployment.html).

## SQL access and scheduled events

SQL observation profiles can be registered while OCI execution stays read-only; saving a username and Vault secret reference changes only the local registration database. Before creation, click **SQL access** on the source row and select a username and an active Vault password secret. Use an account with visibility of all events in all application schemas, permission to read global variables and `performance_schema` replication views, and `EVENT` privileges for event administration. Limited metadata visibility can hide events: verifying the account's grants is an operator prerequisite. Green automatically reuses the paired Blue username and Vault secret reference, including existing deployments without a Green registration. An explicit Green profile takes precedence; creation preserves it. SQL access preselects the inherited profile and explains its source. SQL connections require TLS. Set `BLUEGREEN_MYSQL_SSL_CA` to a server-local CA file to request certificate and identity verification. Endpoint verification connects through the original IP, which must be covered by the certificate when identity verification is enabled.

The creation form always queries `information_schema.EVENTS`, including definitions, definers, schedules and states. Review is required even if the list is empty. You may select individual enabled events or disable all enabled events before creation. The application registers their original states before mutation. On successful creation, it restores originally enabled events to `ENABLED`, leaves originally disabled events `DISABLED`, and preserves unchanged `REPLICA_SIDE_DISABLED` events. If HA is restored, event restoration runs afterwards because primary changes can affect event status.

Switchover requires a fresh event review on both systems. Green's final enable/disable defaults come from the recorded original source states; for a deployment created outside the application, defaults come from the current source review. Operators explicitly approve the final choices and may override them per event. Newly introduced events default to keeping their current state. Event actions run after promoted HA and copied-channel processing, and their final states are checked. Source events selected for disabling stay disabled on the retired source. Other source events keep their state.

Event review fingerprints cover definitions, schedules and states. Changes after review block execution. Disabling an event does not interrupt an invocation already running. SQL uses normal logged `ALTER EVENT` statements; no unsupported `sql_log_bin` bypass is required. Replicated event definitions normally remain replica-side disabled on Green. Precheck rejects enabled Green events until the operator disables them through **Events**. For temporary Green changes, enable read-write explicitly and return it to read-only before promotion.

## Creation and HA

Supply target version, shape, configuration and storage; storage cannot shrink below Blue's size. Supply an existing replication username and a password secret from the chosen secret compartment. The application does not create MySQL users or grant their replication privileges. New password secrets can be created in a selected compartment using an existing Vault and enabled AES key; reopen the creation form once the secret becomes active.

For each existing inbound channel, register its username and Vault password secret. OCI does not return replication passwords. Channels are copied to Green disabled, preserving source/target options and assigning a collision-free channel name against the actual native channel. Copied channels remain separate from the native Blue-to-Green channel. Activating them requires successful promotion and then a button click or an explicit switchover option.

HA sources are converted to standalone before native creation. Option 1 leaves both standalone. Option 2 restores Blue HA with the chosen configuration after creation, leaving Green standalone. Switchover always disables Blue HA again. Enabling HA on promoted Green requires a compatible configuration OCID and explicit selection.

The background monitor inspects only the current selected compartment every 30 seconds, using the VM compartment until the selection changes. A cached unhealthy status causes a journaled fresh status generation; only a fresh `is_healthy=false` result triggers channel disabling. Unknown health never triggers guessed mutations. Failed quarantine attempts remain visible in history and have a five-minute retry cooldown. Unhealthy channels should be fixed or deleted; re-enabling is an explicit operator action. The native channel is controlled by OCI, so manual deletion/enabling is blocked; an unhealthy native channel can cause quarantine failure if OCI disallows the update, and the deployment cannot pass readiness checks.

## Switchover verification

The promoted HA configuration is optional. When **Enable HA on promoted Green** is selected, a supplied override takes precedence, followed by the registered promoted override, registered Blue HA configuration, original Blue HA configuration, current Blue HA configuration, and otherwise the current Green configuration unchanged. The confirmation preview and execution journal show the selected configuration and its source. Leaving HA unselected keeps promoted Green standalone. OCI validates configuration compatibility during the HA update.

Precheck accepts an `ACTIVE` deployment with `NOT_STARTED` or `READY_FOR_SWITCHOVER` while Blue is still the current DB system. `NOT_STARTED` is the normal initial switchover state; OCI reevaluates readiness on submission. Other states block execution and display the actual state. Precheck still requires standalone Green, an enabled healthy native channel, and all other Green inbound channels disabled. GTID comparison uses interval arithmetic: Blue ahead of Green is allowed during replication; extra Green transactions are allowed only under Green's server UUID; any unexpected UUID or extra interval under another UUID fails.

Before handoff the workflow verifies standalone Blue and standalone/read-only Green, repeats readiness checks, records both server identities and the primary endpoint, and calls the native OCI switchover API. OCI owns write blocking, replication drain and endpoint reassignment. Switchover can interrupt connections and cannot be automatically rolled back after success; see [Oracle's switchover documentation](https://docs.oracle.com/en-us/iaas/mysql-database/doc/switching-over-blue-green-deployment.html).

After handoff it verifies OCI's active DB system, connects through the original primary IP, and checks the Green hostname/server UUID and read-write state. It samples the retained Blue DB separately, confirms the original Blue server UUID, and verifies Green contains the final Blue GTID set, including writes drained after the pre-switch snapshot. Exact equality is green; accepted extra Green transactions are orange. If the original/retired endpoint cannot be verified, the job remains failed or processing rather than claiming success. Event re-enabling happens afterwards and can itself generate new Green GTIDs.

## Failure and recovery

Each step records its work-request ID before polling. A two-hour timeout or worker restart does not mean OCI stopped the submitted operation. Inspect the work request and current DB/deployment state before starting another operation. Workflow failures retain completed changes; there is no automatic infrastructure rollback or replay. Event snapshots and original states persist. Use **Events** to restore the recorded enabled/disabled states after reconciling a failed creation, and inspect history for already completed transitions. Event DDL can partially succeed; each event is logged separately.

Enable/disable HA, creation and switchover default to Standard OCI DB-system updates. Operators can select ETag protection in each confirmation dialog. Standalone read/write-mode actions and channel updates retain `If-Match` protection by default. Protected updates request uncompressed representations and send cache-control headers. Live diagnosis on local3310 reproduced different ETags for identical response bodies through both the SDK and direct signed REST. OCI rejected several freshly returned validators, then accepted an exact, unchanged `If-Match` value for HA disable. The app therefore retries only explicit `412 NoEtagMatch` responses, with a fresh GET and capped backoff between attempts: up to 12 DB-system attempts and 3 channel attempts. Every request retains If-Match. Resource details must still match the original snapshot, including after the final rejected attempt; changed details, missing ETags or other errors stop execution. Accepted work requests stop retries immediately and are monitored normally. GET ETags, GET/update request IDs and outcomes are retained in the workflow journal; successful steps expose the attempt list in Details. There is no automatic fallback to unconditional updates, and this conflict does not require broader IAM permissions. The exact internal OCI component causing the inconsistent validators is not established by client-side evidence.

The operation confirmation dialog defaults to **Standard OCI update · without If-Match** for HA enable/disable, creation and switchover. ETag protection remains selectable. Oracle's `UpdateDbSystem` API documents `if_match` as optional. Selecting standard mode applies only to DB-system updates within that one confirmed creation, switchover or HA/database-mode action. It does not change channel updates; the next operation uses its own default. The workflow records the explicit choice in history, checks the selected compartment, requires ACTIVE state and compares two fresh resource snapshots before each update. Each update is submitted once without automatic retries. Standard mode has no atomic server-side version check: another OCI client can change the resource between the final read and the update. The dialog discloses this before confirmation. Creation and switchover eligibility, event review, secret checks and work-request monitoring still run normally. See [Oracle's UpdateDbSystem API](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/mysql/client/oci.mysql.DbSystemClient.html#oci.mysql.DbSystemClient.update_db_system).

For persistent conflicts, run the GET-only diagnostic on a DB system in the VM's default compartment:

```bash
.venv/bin/python validate_etag.py '<source-db-system-ocid>' validation/etag-diagnosis.json
```

It compares response-body hashes and ETags with gzip and uncompressed responses, records request IDs and reads the local journal. It never submits a DB-system mutation. Different ETags for identical bodies indicate inconsistent service validators; provide that evidence and the failed update's request ID to Oracle Support. Passing intercepted tests does not prove that a live retry will succeed.

Do not treat a successful mock or demo check as live tenancy certification. Live HA transitions, Vault IAM, private SQL access, native channel behavior, endpoint reassignment, and event grants must be validated on a non-production deployment in your OCI environment.

## Validation

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app.py modules
node --check static/app.js
```

Tests cover GTID intervals, event-state restoration and choices, stale reviews, authentication and CSRF, credential-free browser session state, journal exclusion/restart handling, TLS options, Connector/Python reuse, and readiness failure paths. `tests/browser_smoke.py` optionally checks the demo with Playwright and writes previews under `/private/tmp/bluegreen-preview`. `tests/test_mysql_integration.py` exercises the actual embedded server when `BLUEGREEN_MYSQL_INTEGRATION=1`. Unit tests inject an explicit test-only store; deployed applications never fall back to SQLite.

## Full OCI execution-path validation without DB mutations

`tests/test_sdk_execution_path.py` enters through the real Flask routes, executes the production workflow and OCI adapter, calls the real OCI SDK methods, and intercepts only the SDK HTTP transport. It checks serialized URLs, HTTP methods, JSON payloads, ETags, retry tokens, SDK response decoding, work-request polling, failure propagation and the final journal. Blue/Green SQL telemetry is supplied by test stand-ins. No DB-system or replication-channel mutation reaches OCI.

On the validation Compute VM, the harness also uses the real instance-principal signer and isolated schemas in embedded MySQL:

```bash
BLUEGREEN_MYSQL_INTEGRATION=1 \
BLUEGREEN_CONTRACT_INSTANCE_PRINCIPAL=1 \
BLUEGREEN_CONTRACT_EVIDENCE=validation/sdk-contract \
.venv/bin/python -m pytest -q --junitxml=validation/tests.xml
```

To preserve an operator's changed password and registrations, run the complete suite in a temporary socket-only MySQL instance using the already installed embedded binary:

```bash
BLUEGREEN_CONTRACT_INSTANCE_PRINCIPAL=1 \
BLUEGREEN_CONTRACT_EVIDENCE=validation/sdk-contract \
.venv/bin/python tests/run_isolated_mysql_validation.py -q --junitxml=validation/tests.xml
```

The temporary server and isolated test schemas are removed on completion. The deployed database and admin account are preserved. The deployed validation console should stay in `--read-only` mode. An operator can deliberately enable real OCI execution later with `./setup.sh --mode https --execute-oci`, after verifying IAM scope and registering the intended DB systems and Vault references.

The report distinguishes live read-only OCI checks, real local MySQL checks, and intercepted mutation-path tests. Passing the contract harness does not claim that OCI's server-side provisioning or endpoint handoff was executed. A live production switchover still requires registered source/target SQL access and explicit operational approval.

## DB detail tabs and operation confirmation

The selected DB system uses **Details**, **Replicas**, **Replication**, **Work Request** and **OCI Configuration** tabs. OCI configuration is grouped into labelled identity, availability, networking, storage, backup, maintenance and service settings. Work requests are paginated with 10 rows by default and selectable page sizes of 10, 25 or 50. The active tab, work-request page, expanded details and focus remain in place during polling.

The console checks for updates every 15 seconds. It keeps existing DOM nodes when displayed content is unchanged; an uptime change updates the uptime value rather than replacing the connection panel. The timestamp indicates the last displayed change.

Before a user-triggered operation runs, a read-only operation preview lists the affected OCI services, resources and planned changes. **Confirm and execute** submits the operation; **Cancel** preserves entered creation parameters. Scheduled-event actions are identified separately as SQL changes, and observation registration is identified as a local registration update. The workflow revalidates current state before execution.

The sticky top banner shows submission, queued/running steps and the current work-request ID, followed by success or the failed step and error. Running feedback is orange; normal completion is green. A failure turns the notification bell red until acknowledged, including while a subsequent operation runs. Clicking the bell displays that failure on the banner. **View activity** opens the operation's persistent journal; dismissing the notification acknowledges it without removing any history. Unchanged status polls preserve the displayed content and do not reopen an acknowledged notification.

Operations shows only active/queued application workflows, or the most recent workflow when idle. The full OCI work-request list is omitted there. Activity history retains all application operations. The selected DB system's paginated Work Request tab includes its own requests, its paired DB system, related Blue Green deployment/channels and journaled operations. OCI `ListWorkRequests` summaries omit resource identifiers, so the adapter retrieves resource associations with `GetWorkRequest`, caches completed mappings and refreshes active mappings at most once per 15 seconds. Unverified associations are reported as informational and do not broaden the selected-compartment scope.

For a registered DB system, Blue and Green server buttons appear at the top of its detail panel with white text on the corresponding blue/green background. The current selection and primary are marked separately. Clicking the paired server selects its details, switches the HA/standalone list when needed and clears only filters that would hide the target. Deleted partners are omitted; unavailable or out-of-compartment partners are not clickable.

Work-request details require a successful `GetWorkRequest` in the selected compartment. Error-list reads are independent: their failure returns the known OCI status with an informational diagnostic notice and an explicit availability flag. An unavailable error list is not treated as a verified empty list. Optional OCI work-request progress logs are not fetched or displayed; the application uses its own persistent workflow steps, timings and details for execution history. These OCI progress logs are separate from MySQL server logs. Removing their retrieval does not affect work-request status polling, error inspection, creation or switchover. The dialog shows any failed error-read operation and request ID, plus compartment-specific access guidance. A real OCI FAILED status remains red even if its error details are unavailable. A 404 alone cannot distinguish missing authorization from an unavailable endpoint/resource; do not widen policy scope on that evidence alone.

Live operation is enabled by `./setup.sh --mode https --execute-oci`. This permits real OCI mutations and starts the confirmed-unhealthy-channel monitor, scoped to the current selected compartment. It does not grant IAM permissions. The earlier read-only validation mode can be restored with `./setup.sh --mode https --read-only`.

The Details tab reads MySQL version, version comment, database mode, access mode, hostname and uptime directly through SQL. Database mode uses global `read_only` / `super_read_only`; access mode uses `offline_mode`. HA, shape, configuration and endpoint metadata come from OCI. Configurations display as `Display Name [OCID]`; unavailable names show an informational notice. Variables and Status tabs run `SHOW GLOBAL VARIABLES` and `SHOW GLOBAL STATUS` for the selected DB system, with name/value search and 10/25/50/100 rows per page. Only the visible SQL tab is polled; unchanged results preserve the DOM, filters and page. Connection failures clear previous SQL values and show `*****`. Passwords and credential-bearing variable values are masked server-side. These read-only tabs use the registered SQL username and Vault secret, including inherited Green access.
