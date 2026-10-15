# OCI MySQL DB-system updates: ETag usage and removal

Updated 15 October 2026 · [Project overview](../README.md) · [Technical guide](../docs/technical-guide.md)

The application now uses **Standard OCI requests for every operation**. ETag execution, conditional retries and the UI mode selector have been removed. This document preserves the former behavior and reproduction examples; the examples are not application execution paths.

## What ETag did

An ETag is a resource version validator, not an authentication token. The former code read a DB system with `get_db_system()`, then passed the returned `etag` as `if_match` to `update_db_system()`. OCI was asked to accept the update only if that validator still matched. Instance Principal continued to handle authentication. The `if_match` argument is optional. [Oracle DB-system SDK reference](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/mysql/client/oci.mysql.DbSystemClient.html#oci.mysql.DbSystemClient.update_db_system).

## Recorded behavior and decision

| Operation | Recorded outcome |
| --- | --- |
| Enable or disable HA with ETag | Repeated `412 NoEtagMatch`, including attempts that fetched a new validator and observed unchanged returned resource details. |
| Set read-write with ETag, 14 October 2026 at 22:40 UTC | Failed after three attempts; no work request was returned. |
| Set read-write with Standard, 14 October 2026 at 22:42 UTC | Succeeded and completed its work request. |
| Recorded read-only changes | Succeeded using Standard; no live ETag read-only result was available in the inspected journal. |

Later code increased the conditional DB update limit to twelve attempts and requested uncompressed, uncached responses. Further HA failures were recorded. Repeated conditional attempts therefore did not provide a reliable operational path in this environment. The operator requested Standard requests throughout the application.

**The underlying mismatch was not conclusively diagnosed.** Unchanged JSON resource details do not prove that OCI's internal resource version stayed unchanged. The records do not establish that dashboard refreshes caused the failures. A GET is a read operation; no extra prerequisite call beyond obtaining the resource validator is specified for `if_match`. Oracle Support would need the paired GET/update request IDs to investigate further. The removal is an application decision, not proof that OCI ETags fail universally.

A separate `500 InternalError` occurred during an HA update without ETag. Removing the conditional header does not prevent unrelated configuration or service failures.

## Sample code

Run from an authorized OCI Compute VM with the OCI Python SDK and Instance Principal access to the selected compartment. Replace the placeholders locally. These functions change a real DB system if invoked; the calls below remain commented so copying the example does not submit an operation.

```python
import oci

DB_ID = "<db-system-ocid>"
COMPARTMENT_ID = "<db-compartment-ocid>"
signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
client = oci.mysql.DbSystemClient({"region": signer.region}, signer=signer)

def checked_read():
    response = client.get_db_system(DB_ID)
    if response.data.compartment_id != COMPARTMENT_ID:
        raise ValueError("DB system is outside the selected compartment")
    if response.data.lifecycle_state != "ACTIVE":
        raise ValueError("Wait for the DB system to become ACTIVE")
    return response

def historical_etag_update(change):
    before = checked_read()
    validator = before.headers.get("etag")
    if not validator:
        raise ValueError("GET returned no ETag")
    print("GET request ID:", before.headers.get("opc-request-id"))
    return client.update_db_system(
        DB_ID, oci.mysql.models.UpdateDbSystemDetails(**change),
        if_match=validator,
        retry_strategy=oci.retry.NoneRetryStrategy(),
    )

def standard_update(change):
    baseline = checked_read()
    current = checked_read()
    if oci.util.to_dict(current.data) != oci.util.to_dict(baseline.data):
        raise ValueError("Resource changed; review before retrying")
    return client.update_db_system(
        DB_ID, oci.mysql.models.UpdateDbSystemDetails(**change),
        retry_strategy=oci.retry.NoneRetryStrategy(),
    )

# Choose ONE change and ONE function for a controlled test:
change = {"is_highly_available": False}  # Disable HA
# change = {"is_highly_available": True}  # Enable HA
# change = {"database_mode": "READ_ONLY"}
# change = {"database_mode": "READ_WRITE"}
# try:
#     response = standard_update(change)
#     # Or historical_etag_update(change) to reproduce the former path.
#     print("Update request ID:", response.headers.get("opc-request-id"))
#     print("Work request:", response.headers.get("opc-work-request-id"))
# except oci.exceptions.ServiceError as error:
#     print(error.status, error.code, error.request_id)
```

Enabling HA still requires a compatible DB configuration. Apply a configuration change separately and wait for completion before enabling HA; do not combine an unchanged configuration with the HA toggle. A returned update response is acceptance, not completion: monitor its work request, then re-read the DB system before a reverse operation. Read-only/read-write use `database_mode` in the OCI API, not SQL `SET GLOBAL`.

## Current application behavior

- HA, read-only/read-write, and workflow DB updates all use Standard requests. Replication-channel enable/disable also omits the optional condition. [Oracle channel update reference](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/mysql/client/oci.mysql.ChannelsClient.html#oci.mysql.ChannelsClient.update_channel).
- The application verifies compartment, lifecycle state and unchanged returned details before DB-system and channel updates. Each update is submitted once, with SDK mutation retries disabled.
- Confirmation dialogs still list the planned changes. Work requests and failures remain in Activity history; historical ETag failures are retained as audit records.
- Older clients requesting a removed update mode receive a refresh/review error before execution. The backend cannot select ETag execution.
- Standard requests have **no atomic version check**. Another OCI client can change a resource between the final GET and the update. Coordinate changes with other operators.

SDK transport tests assert that application mutations carry no `If-Match`, exercise both read/write directions, and check that failed updates are not replayed. Intercepted tests verify the code path; they do not certify a future live OCI result.
