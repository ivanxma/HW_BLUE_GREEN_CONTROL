# Technical guide

[Project overview](../README.md) · [Architecture](architecture/heatwave-control.png) · [Architecture (.drawio)](architecture/heatwave-control.drawio)

This guide describes the prototype's operation and the service behavior reviewed in October 2026. Rehearse with representative data before applying a production change. Native OCI eligibility and readiness checks remain authoritative.

## Architecture and network

The Flask web app and one workflow worker run on an Oracle Linux 9 Compute VM. Instance Principal authenticates OCI API requests. MySQL Connector/Python obtains managed-database passwords from OCI Vault and opens TLS SQL connections. Embedded MySQL stores registrations, event snapshots, plans and execution history through a local Unix socket; it has no TCP listener. MySQL Shell is installed for operator use.

The diagram shows an illustrative single-region deployment: a public administration subnet and a private DB endpoint subnet within one VCN. Regional HeatWave and Vault services sit outside the customer VCN; each DB system attaches through its private subnet endpoint. The diagram's HeatWave symbols represent the MySQL service, with no attached analytical HeatWave cluster. The generic official Private Endpoint IP symbol denotes a subnet attachment, not an additional service.

| Connection or rule | Purpose |
| --- | --- |
| Administrator CIDR → VM, TCP 443 | Web application over HTTPS; restrict ingress in both the OCI NSG/security list and host firewall. |
| Administrator CIDR → VM, TCP 22 | SSH administration when needed. |
| VM → DB endpoint NSG, TCP 3306 | SQL telemetry and reviewed event administration; use the actual DB port if different. |
| Green → Blue SQL endpoint | Green initiates the native replication connection; transactions flow from Blue to Green. Retain the service-required replication rules. |
| VM → regional OCI APIs, TCP 443 | Instance Principal control calls and Vault secret reads through a service gateway for supported Oracle Services Network destinations. |
| Public subnet: `0.0.0.0/0` → internet gateway | Public HTTPS access and outbound installation downloads from GitHub/Oracle. The VM needs a public IP for this illustrated route. |
| Public subnet: regional Oracle Services Network service CIDR → service gateway | Private routing to supported regional service endpoints. |
| Private subnet | VCN-local access to DB endpoints; no direct internet ingress. |

The CIDRs and addresses are examples, not a discovery of your tenancy. A private administration VM is also possible with VPN/private access and an appropriate outbound route. DNS resolution for OCI APIs and database endpoints must work. Dynamic-group membership is an IAM construct; its policies grant access only to the specified resource compartments.

## Installation and configuration

### Oracle Linux 9

Run the README setup command as `opc`, with sudo available for prerequisites, systemd and firewall configuration. Python 3.12 or newer is required. Setup downloads app-local MySQL Server and Shell archives, checks their published checksums, initializes the socket-only registration store and installs:

```bash
sudo systemctl status heatwave-bluegreen-local-mysql.service
sudo systemctl status heatwave-bluegreen-https.service
```

The web listener defaults to HTTPS 443. Setup creates a self-signed certificate under `tls/`; set `SSL_CERT_FILE` and `SSL_KEY_FILE` in `.runtime.env` to use a trusted certificate. OCI ingress rules must be configured separately. `oci_compute_init.sh` supports first-boot installation on OL9 and defaults to this repository, its root directory and HTTPS 443.

Use `--read-only` to permit discovery and registration while blocking managed-resource and event mutations. Use `--execute-oci` to enable operations explicitly. Every operation still displays its planned changes for confirmation. Setup reruns preserve the admin password and registrations. Do not run setup or restart the web service during an active workflow.

| Location / setting | Meaning |
| --- | --- |
| `.runtime.env` | Protected deployment settings. |
| `.data/mysql`, `.data/run/mysql.sock` | Embedded database files and local socket. |
| `.data/store.json` | Local store service-account credential; mode `0600`, never committed. |
| `.embedded/` | Downloaded MySQL Server and Shell runtimes. |
| `./mysqlsh.sh` | Wrapper for the embedded MySQL Shell. |
| `BLUEGREEN_COMPARTMENT_ID` | Optional default-compartment override; otherwise use the VM compartment from instance metadata. |
| `BLUEGREEN_MYSQL_SSL_CA` | Optional server-local CA file for SQL certificate and hostname verification. |

Keep MySQL-aware backups of the registration database and protected deployment configuration. A binary rollback does not imply a MySQL datadir downgrade is supported.

### Compartment-scoped IAM

Match only the administration VM in a dynamic group, for example:

```text
ALL {instance.id = '<compute-instance-ocid>'}
```

For read-only discovery:

```text
Allow dynamic-group <vm-group> to read mysql-family in compartment id <db-compartment-ocid>
```

For reviewed execution, replace the discovery grant with:

```text
Allow dynamic-group <vm-group> to manage mysql-family in compartment id <db-compartment-ocid>
Allow dynamic-group <vm-group> to read secret-bundles in compartment id <secret-compartment-ocid>
```

If creating secrets through the UI, also grant the required Vault/key operations in that specific compartment:

```text
Allow dynamic-group <vm-group> to manage secrets in compartment id <secret-compartment-ocid>
Allow dynamic-group <vm-group> to read vaults in compartment id <secret-compartment-ocid>
Allow dynamic-group <vm-group> to use keys in compartment id <secret-compartment-ocid>
```

Network permissions for DB creation must cover the selected subnet and NSGs in their owning compartment. Attach policies where they can grant access to those compartments. The app does not request tenancy-wide MySQL access or scan other compartments for registrations. If access fails after changing compartments, grant access to that selected compartment. OCI `404 NotAuthorizedOrNotFound` alone does not distinguish an absent resource from insufficient permission. See [Instance Principal](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm) and [native creation prerequisites](https://docs.oracle.com/en-us/iaas/mysql-database/doc/creating-blue-green-deployment.html).

### SQL access

Register Blue's SQL username and a Vault password secret. Green inherits that profile unless separately configured. The app does not create database users or grant replication privileges. Use accounts with the required replication permissions, visibility of application events, global variables/status and replication instrumentation; event changes additionally require `EVENT` privileges. Limited metadata visibility can hide events, so check the account's grants.

## Preparing schema changes on Green

This is an operator-controlled workflow. The app provides read/write controls and observation; it does not execute arbitrary schema DDL or automatically choose type-conversion settings.

1. Create Green and confirm healthy replication. Capture the intended schema changes and application compatibility requirements.
2. Select **Allow read-write** on Green and confirm the operation. Keep production application writes directed to Blue.
3. Apply the reviewed DDL to Green using a SQL client. Monitor space, replication lag and applier errors while it runs.
4. Test representative queries and application behavior against Green. Wait for replication to catch up and review scheduled events.
5. Run precheck and review switchover. The workflow returns Green to read-only before invoking OCI's endpoint handoff.

Large index builds consume resources even when concurrent DML is allowed. Column additions can be instant, while type changes and some width changes rebuild/copy data; locking and storage requirements depend on the exact operation. Choose the DDL algorithm deliberately. [MySQL 9.7 online DDL](https://dev.mysql.com/doc/refman/9.7/en/innodb-online-ddl-operations.html).

### Different table definitions and conversions

For differing column counts, common columns must retain their order and precede added columns; extra columns need defaults. Attribute promotion has a separate restriction: the replica cannot have more columns than the source. Plan an added-column change and a type-promotion change accordingly. [Differing table definitions](https://dev.mysql.com/doc/refman/9.7/en/replication-features-differing-tables.html).

HeatWave exposes `replica_type_conversions` in its DB configuration. Create/copy a shape-compatible custom configuration, set the required value and apply it to **Green** through OCI. Configuration variables cannot be edited in place. Review any restart impact and verify the applied value in the Variables tab. [HeatWave configuration variables](https://docs.oracle.com/en-us/iaas/mysql-database/doc/configuration-variables.html).

| Setting | Intended use |
| --- | --- |
| Empty/default | No attribute promotion or demotion. |
| `ALL_NON_LOSSY` | Supported widening conversions. |
| `ALL_NON_LOSSY,ALL_UNSIGNED` | Candidate for reviewed integer promotion with unsigned interpretation. |
| `ALL_LOSSY` | Permits supported conversions that may truncate values; not a general compatibility fix. |

For an `INT` → `BIGINT UNSIGNED` auto-increment change, validate existing and future value ranges, negative values, foreign keys, application bindings and the complete column definition. Preserve required attributes such as `AUTO_INCREMENT`, nullability and comments. Enabling conversions does not validate those dependencies. [Attribute promotion and signedness](https://dev.mysql.com/doc/refman/9.7/en/replication-features-differing-tables.html#replication-features-attribute-promotion).

Read the effective Green settings before and after configuration changes:

```sql
SELECT @@version, @@global.replica_type_conversions,
       @@global.read_only, @@global.super_read_only;
SHOW CREATE TABLE application_schema.large_table;
```

These settings affect row-based replication across the server's channels, not just one table. Keep other inbound Green channels disabled during preparation. The app can select an existing target configuration during creation; later custom-configuration changes are performed through OCI. See [OCI configuration model](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/mysql/models/oci.mysql.models.ConfigurationVariables.html).

## Creation, HA, channels and events

Native Blue/Green supports same-region standalone systems. HA systems, attached HeatWave clusters, MySQL REST Service and Always Free have native restrictions. This app also blocks creation when read replicas are present. Its HA sequencing does not turn native Blue/Green into an HA-supported service. [Oracle service scope](https://docs.oracle.com/en-us/iaas/mysql-database/doc/blue-green-deployments.html).

Creation records the source settings, optionally disables HA, creates the native deployment, waits for OCI work requests and confirms Green is read-only. An operator may restore Blue HA after creation; Green stays standalone. Select target version, shape and configuration from OCI-backed dropdowns. Storage cannot shrink below Blue's size. Configuration names and OCIDs are displayed together. A changed target shape needs a compatible promoted HA configuration; the original compatible configuration can otherwise be reused.

Existing inbound channels need separately registered usernames and Vault secret references because OCI does not return their passwords. The app copies them to Green **disabled**, avoiding native-channel name collisions. Activation is a separate reviewed action after promotion or an explicit switchover option. Freshly confirmed unhealthy channels can be disabled by the background monitor when live operations are enabled; unknown health is never treated as proof of failure. Fix or delete unhealthy channels before continuing.

Creation inventories scheduled events and requires review even for an empty list. The selected enabled events can be disabled temporarily. Their original states are recorded and restored after successful creation. A failed workflow retains the snapshots for operator reconciliation. Disabling an event does not stop an invocation already running.

Switchover reviews source and target events again. The user selects the final Green enabled/disabled states, initially based on the recorded source states. Enabled Green events must be disabled for precheck. Event states are applied and verified after promotion and requested post-processing. Stale event reviews block execution.

## Switchover and verification

Precheck verifies an active deployment, Blue as the current system, standalone Green, healthy native replication and no other enabled inbound Green channels. Blue may be ahead while replication catches up. Additional Green GTIDs are accepted only under Green's own server UUID; unexpected GTIDs fail the check.

The workflow disables Blue HA when needed, returns Green to read-only and repeats readiness checks. OCI then controls write blocking, replication drain and primary-endpoint reassignment. After handoff, the app verifies the promoted OCID, SQL hostname/server UUID, the original endpoint, final Blue GTIDs on Green and Green's read-write state. Names may swap; identity checks use OCIDs and SQL UUIDs.

Optional post-processing enables HA on the promoted system, activates registered copied channels and applies reviewed event states. Timings and work-request IDs are logged for every step. A blank promoted HA override reuses a compatible registered/original configuration. If native switchover succeeded but a later step failed, reconcile that remaining step; do not rerun the switchover. OCI may interrupt client connections and does not automatically switch back after success. [Native switchover behavior](https://docs.oracle.com/en-us/iaas/mysql-database/doc/switching-over-blue-green-deployment.html).

## Dashboard and recovery

The menus are Blue/Green Deployment, DB Systems and Activity history. Both server panels share Details, Replicas, Replication, Work Request, Variables, Status and OCI Configuration tabs. SQL supplies version, read/write/access mode, hostname, uptime and GTIDs; OCI supplies HA, shape, configuration and endpoints. Failed observation clears stale live values to `*****`. Search, page and tab selections persist across unchanged polling. Deleted resources are hidden.

Normal status text is green, active work is orange and failures are red. Blue/Green server names have their role color. A top banner shows operation progress and failures. The latest or active operation is displayed alongside deployment details; Activity history retains the complete journal. Work requests include the selected system and its related deployment resources.

All operations use **Standard OCI requests**. DB-system updates check fresh ACTIVE state, the selected compartment and unchanged details before submission. Channel updates also check the selected compartment, a settled lifecycle state and unchanged details. Each update is sent once without an atomic version condition or automatic mutation retries. Another client can still change a resource between the check and submission. The confirmation dialog lists changes without an update-mode selector. See [the former ETag behavior, examples and removal rationale](etag-usage.md).

Run one application worker. Restarting during a workflow marks it interrupted; a timeout/restart does not cancel an OCI request already submitted. Inspect the work request and journal before retrying. Completed changes are retained, with no automatic rollback or replay. Credentials and reports are excluded from Git.

## Lifecycle and configuration changes

Switchover is available only while the deployment is ACTIVE, has not switched over, and its source (Blue) OCID is still the primary. Names can change after promotion; eligibility uses OCIDs. Deleting a deployment removes only that deployment. Both DB systems and existing channels remain. [Oracle deployment management](https://docs.oracle.com/en-us/iaas/mysql-database/doc/managing_a_blue_green_deployment.html).

Each DB system offers Start, Stop and Delete. Stop uses FAST shutdown and requires confirmation. Delete is enabled only after OCI reports deletion protection disabled; disabling protection is a separate confirmed action. The deletion confirmation identifies the DB OCID and its backup policy. Lifecycle confirmations expire after ten minutes and are checked again before execution. These actions use Standard OCI requests and journal their work requests.

**Edit configuration** shows configured values with search and paging. Additions are restricted to documented HeatWave user variables. Enumerations, Boolean values, sets and flags use selections; numeric values retain full precision. Service-managed and initialization values remain read-only. The catalog records its Oracle reference and review date; OCI still checks version and shape-specific constraints.

Save creates the next `[original_name]_v###` configuration in the selected compartment, preserving the verified base configuration and shape. Custom overrides are carried into the new version; the original stays unchanged. After saving, choose **Apply** or **Later**. Apply needs a separate confirmation and validates shape, HA compatibility and initialization values; OCI may restart the DB system. The journal records the new configuration OCID even if subsequent monitoring fails.

Remove an optional variable only after acknowledging that omission does **not** reset a persisted value on an existing DB system; inherited/default values may still apply. Changing its value is preferred. HeatWave-required variables must remain. [Configuration variables](https://docs.oracle.com/en-us/iaas/mysql-database/doc/configuration-variables.html).

The Green read-write confirmation explains `replica_type_conversions` and offers a shortcut to its configuration field. Empty, `ALL_NON_LOSSY`, `ALL_LOSSY`, `ALL_SIGNED` and `ALL_UNSIGNED` are described, including truncation risk and signedness combinations. The notice makes no automatic configuration change. [MySQL conversion rules](https://dev.mysql.com/doc/refman/9.7/en/replication-features-differing-tables.html#replication-features-attribute-promotion).

## Validation

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app.py modules
bash -n setup.sh oci_compute_init.sh ol9/*.sh
```

The SDK contract tests exercise Flask → workflow → OCI adapter → real SDK serialization with cloud HTTP requests intercepted. They do not provision or switch live DB systems. `tests/run_isolated_mysql_validation.py` can run the suite against an ephemeral embedded MySQL instance while preserving the operator store. Browser tests cover tabs, paging, stable refreshes and operation confirmations. Passing these checks is not certification of a production switchover in another tenancy.
