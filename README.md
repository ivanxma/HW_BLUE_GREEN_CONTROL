# HeatWave Blue Green Control Room

## Background

A prototype for advanced use of **Blue/Green Deployment with OCI MySQL HeatWave**. It brings preparation, observation and switchover into one web application running on an Oracle Linux 9 Compute VM.

The aim is to prepare and test changes on Green while Blue continues serving production, then perform a reviewed switchover. The app coordinates OCI operations, SQL checks, scheduled events and activity logging. It is a prototype for evaluation and rehearsal, not an Oracle-supported product.

## Challenges — October 2026

Native HeatWave Blue/Green Deployment requires **standalone DB systems**; it does not support HA DB systems. This prototype can disable HA before creation, optionally restore Blue HA during preparation, and disable it again for switchover. Those intervals have reduced availability. Other native exclusions include attached HeatWave clusters, MySQL REST Service, Always Free and cross-region deployments. [Oracle service scope](https://docs.oracle.com/en-us/iaas/mysql-database/doc/blue-green-deployments.html).

Large-table changes can be expensive on production:

| Change to prepare on Green | Potential production impact |
| --- | --- |
| Build a large index | Long execution and substantial CPU, I/O and storage demand. |
| Append a column to a huge table | A rebuild or metadata-lock wait can be disruptive when an instant operation is unavailable. |
| Increase a column's width | Some changes are online; others require copying the table. |
| Expand an `AUTO_INCREMENT` key from `INT` to `BIGINT UNSIGNED` | A type change can require a lengthy table copy and dependent schema changes. |

The exact impact depends on the definition and DDL algorithm; adding a column is often instant in MySQL 9.7. [Online DDL behavior](https://dev.mysql.com/doc/refman/9.7/en/innodb-online-ddl-operations.html).

Green can temporarily be made writable for reviewed changes. Its schema must remain compatible with incoming replication. A Green HeatWave configuration can set `replica_type_conversions` for supported conversions; this does not make arbitrary schema differences safe. See [MySQL attribute promotion](https://dev.mysql.com/doc/refman/9.7/en/replication-features-differing-tables.html#replication-features-attribute-promotion) and the [technical guide](docs/technical-guide.md#preparing-schema-changes-on-green).

## Installation

**Oracle Linux 9 is the only tested and supported platform.**

```bash
git clone https://github.com/ivanxma/HW_BLUE_GREEN_CONTROL.git bluegreen_control
cd bluegreen_control
./setup.sh --platform ol9 --mode https --port 443
```

Run as `opc` with sudo access. Provide compartment-scoped instance-principal permissions, private SQL connectivity to the DB systems, outbound HTTPS, and TCP 443 ingress restricted to your administrator network. Setup installs app-local MySQL Server and MySQL Shell. See [installation and IAM details](docs/technical-guide.md#installation-and-configuration).

Open `https://<compute-address>`. Sign in as `admin` with initial password `ChangeMe123!`; the first login requires a password change. Replace the generated self-signed certificate for shared use. For discovery-only evaluation, add `--read-only` to setup.

## Details

![Oracle-style architecture: OL9 control VM, private DB endpoints, regional HeatWave and Vault services, and compartment-scoped IAM](docs/architecture/heatwave-control.png)

[Editable draw.io diagram](docs/architecture/heatwave-control.drawio) · [Technical guide](docs/technical-guide.md)

- Side-by-side Blue/Green details, live SQL version/mode/GTIDs, and searchable Variables and Status tables.
- Reviewed HA, event and replication-channel handling, with paginated work requests and persistent activity history.
- Standard OCI requests for HA, read/write controls, creation, switchover and replication-channel updates.

The diagram uses illustrative networking and official OCI symbols. Oracle styling does not imply endorsement. Reports, credentials and generated runtime data stay outside Git.
