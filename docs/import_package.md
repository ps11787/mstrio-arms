# Import Package Script

Set `MSTR_PASSWORD` in the execution environment before running the import,
package-creation, or project-sync scripts; these scripts no longer supply a
hard-coded password default.

This document explains the workflow of [import_package.py](../import_package.py) and how it relates to [create_package_validation_loop.py](../create_package_validation_loop.py).

## Purpose

The import script is the downstream step after package creation. The creation
script builds a source migration package and records its metadata without reading
a target project or running target validation. The import script resolves the
destination and imports the existing package.

In short:

1. [create_package_validation_loop.py](../create_package_validation_loop.py) creates the package.
2. It records package metadata and marks the request as package-created.
3. [import_package.py](../import_package.py) loads the existing migration object and executes the import into the target project.

## Workflow

### View-based entry point

#### Reverse requests (status 18)

The view-based entry point dispatches status `18` to `Migration.reverse()`, the
SDK equivalent of Workstation's Reverse action (request and approve undo).
Before starting, it refreshes the migration's state. A `PENDING` undo request
uses `reverse()`; an already `REQUESTED` undo is approved directly, avoiding the
SDK's duplicate requested-to-requested transition. An `APPROVED` or `UNDOING`
operation is only monitored, and `UNDO_SUCCESS` is not submitted again.
Unknown/rejected undo requests and previous `UNDO_FAILED` results fail explicitly.
It polls until `UNDO_SUCCESS`; an old `IMPORTED` status is not rollback success.
API errors, `UNDO_FAILED`, and timeout are failures. Request and existing latest
migration-history row receive status `11` on failure, plus a comment and
matching `migration_message`. A successful rollback inserts a new history row
with status `19`, the same message, and the request's migration key/project.
The existing latest row is left untouched on success so the rollback becomes a
new auditable attempt.

The deployed view includes status 18 requests.
Rollback inputs use the highest `migration_id` for the request, that history
row's `migration_key_id`, its currently linked `migration_obj_id`, and its stored
target `project_id`. They do not resolve a destination from status 18 or use the
request's current migration key. This intentionally includes the latest failed
attempt if present. History does not preserve immutable Strategy GUIDs per
attempt, so the current linked migration object is reversed, as agreed.
Missing/ambiguous rollback mappings fail explicitly; no alternate target or
earlier attempt is chosen. Undo must be available on that migration object.
Do not run concurrent workers that modify these cached request/history mappings.

[import_package_from_view.py](../import_package_from_view.py) is an alternative
entry point. The original [import_package.py](../import_package.py) remains
unchanged and supplies shared configuration and migration helpers; keep both
files together. Run the alternative with:

```powershell
.\.venv\Scripts\python.exe .\import_package_from_view.py
```

It fetches the 15 view columns once into a list, newest first, and processes the
first request with a migration reference, just like the original selection.
It skips missing packages, but does not skip mapping failures in favor of a
later ready row. Empty inputs print a message and exit successfully.
Readiness flags accept database booleans and datasource `0`/`1` strings.
Invalid mappings print the configured resolution message, insert its comment,
and update the request and cached latest history row using the view's failure
status before raising the error. No history row is fabricated for unresolved
targets.

All database lookup inputs come from the view; there are no subsequent standalone
SELECTs or request-key subqueries in writebacks. Only next-ID subqueries remain
inside INSERTs, as in the original workflow. History and reused-package updates
use the cached migration key. The cached latest history ID refers to the initial
read; this script does not implement concurrent-worker claiming or locking.
Locked-package reuse, completion polling, post-import/undo checks, result statuses,
history inserts, comments, and connection cleanup retain the original helpers
and behavior. MicroStrategy API calls still occur during migration.

This entry point uses the view's strict resolution for imports and fixed status list
`(5, 6, 7, 18)`; legacy resolution and request-status-filter settings do not override
the SQL. URL fallbacks still use the shared environment configuration.
It explicitly selects the 15 columns of the deployed view. It does not deploy a view or start
the original script's `main()`.

### Standalone consolidated input view

[create_import_package_input_view.sql](../create_import_package_input_view.sql)
defines the PostgreSQL view `arms.v_mig_import_input`. Deploy the SQL separately
with an account allowed to create views in `arms`. Generating and validating this
definition does not deploy it or change the import script's existing queries.
The database has the reduced 15-column import/reverse view. Replacing an older,
wider view requires dropping and recreating it; `CREATE OR REPLACE VIEW` cannot
remove existing columns. Check dependent objects and preserve grants before
redeploying.

To inspect the inputs (the view-based entry point selects the 15 columns explicitly):

```sql
SELECT *
FROM arms.v_mig_import_input
ORDER BY request_id DESC;
```

The output is limited to 15 import/writeback fields: `request_id`,
`request_status_id`, `source_project_name`, `migration_key_id`,
`migration_obj_id`, `tgt_env_id`, `target_env_url`, `target_project_name`,
`tgt_project_id`, `status_id_success`, `status_id_failed`,
`resolution_failed_status_id`, `latest_migration_id`, `resolution_error`, and
`is_ready`. Migration names and timestamps are fetched from the runtime migration
object when needed, not from the lookup table.
Target resolution uses request status, mapped environment, its configured prefix,
and the source project-name suffix; a request's stored target ID is not an override.

Inspect `is_ready` and `resolution_error` before importing. Do not filter invalid
rows out of the initial read: keep them for error comments and failed-status
writebacks using `resolution_failed_status_id` (environment failure status, or
`11` when missing). Lookup counts and prefix/suffix checks remain internal to the
view; `resolution_error` exposes their failures without returning helper columns. Unlike
legacy first-row lookups, duplicate source, status, environment, or migration
object mappings explicitly make a request not ready.

The view includes only requests whose `t_mig_dim_request.status_id` is in
`(5, 6, 7, 18)`. It does not embed credentials, application URL
fallbacks, or next IDs for inserts. Writes still allocate IDs at write time and
must account for concurrent changes; `latest_migration_id` is only the value at
read time. This is a normal view, not a materialized cache. A future single-query
consumer can reduce network round trips, but query-speed improvements require
measurement against the production workload.

### Current script workflow

The script follows these steps:

1. Connect to the source environment through `Connection`.
2. Query requests in `MSTR_REQUEST_STATUS_FILTER`, newest request first, using SQL.
3. Join `arms.t_mig_dim_request.project_id` to `arms.t_mig_lu_project.project_id`
   to retrieve `project_name AS source_project_name`. A left join preserves
   requests with missing source mappings so resolution errors can be recorded.
4. Select the first request with an available migration reference from the
   migration-object lookup. All input values belong to that selected request;
   `Request_list_status_2` and `MSTR_REQUEST_REPORT_ID` are no longer used.
5. Connect to the target environment using the target project name.
6. Load the migration object with `Migration(my_conn, id=migration_obj_guid)`.
7. Call `migrate()` to import the package into the target project.
8. Use `status_id_success` or `status_id_failed` from the selected target environment
   in `arms.t_mig_lu_tgt_env`.
9. Update `arms.t_mig_dim_request.status_id` for the current request.
10. Close both connections in the `finally` block.

## Expected Behavior

### Successful import

On package-import failure (including connection/start, completion, and post-import
check errors), both entry points insert the exact failure comment text into
`arms.t_mig_dim_migration.migration_message` on the new failed-attempt row.
Quotes are SQL-escaped without changing the stored message. The column must exist
in the database; these scripts do not alter the schema. Successful inserts omit
the column, preserving its database default. Pre-import mapping failures continue
to use the existing comment/status-update path rather than inserting an attempt.

If the migration reaches `IMPORTED` and the required post-import checks pass, the
script inserts a migration-history record and updates the request using the
target environment's `status_id_success`. An undo result is not treated as a
successful import.

### No eligible requests

If the request SQL query returns no rows, the script raises `ValueError` and stops.

### Missing migration object GUID

If `Migration Obj@GUID` is empty, the script raises `ValueError`. The import cannot start because there is no package identifier.

### Target project resolution

`FORCE_STATUS_TARGET_PROJECT_RESOLUTION` near the beginning of the script defaults to
`True`. It can also be configured with the environment variable
`MSTR_FORCE_STATUS_TARGET_PROJECT_RESOLUTION` (`true` or `false`).

When enabled:

1. The selected request must have a `status_id`.
2. That status must map to a target environment in `arms.t_mig_lu_status`.
3. The script queries `arms.t_mig_lu_tgt_project` using both the request's
   `status_id` and the mapped `tgt_env_id`.
4. It filters those rows by the prefix configured on the target environment.
   Matching is case-insensitive and requires the prefix to end at a separator
   or the end of the name (`T01` does not match `T010`).
5. It removes the source prefix up to the first ` - ` separator (for example,
   `E01 - MicroStrategy Tutorial` becomes `MicroStrategy Tutorial`). An unprefixed
   source name is preserved. The target name after its configured prefix must
   equal that source suffix, case-insensitively; unrelated same-prefix projects
   are excluded. A missing source name is an error, not a target-name fallback.
6. Exactly one distinct, non-empty target project name must remain. A missing
   environment prefix, no matching project, or ambiguous matches raise
   `ValueError` before connecting to the import target.

Resolution errors print diagnostic details and add this exact comment to the
selected request before closing the source connection and propagating the error:
`Unable to resolve matchine Target Project name. Please check Environment/Status/Project mapping.`
No import is attempted on this path. After writing the comment, the request and
its latest existing migration-history record are updated to the environment's
`status_id_failed`. If the environment or its failure-status mapping cannot be
resolved, `TARGET_RESOLUTION_FAILED_STATUS_ID` (default `11`) is used instead.
Older history is preserved. If no migration-history record exists, only the
request is updated; no record is inserted with an unknown target-project key.
Comment/status writeback failures are reported explicitly.

The target-project table's `status_id` is a destination-selection mapping.
Result writeback still uses the target environment's `status_id_success` and
`status_id_failed`, not the target-project status.

Request/report target names, request target-project IDs, and derived
names cannot override this mapping. For both successful and failed attempts,
`t_mig_dim_migration.project_id` is the numeric `tgt_project_id` from
`arms.t_mig_lu_tgt_project` (or `MSTR_TARGET_PROJECT_TABLE`). It is looked up by
the resolved destination project name, not taken from the request, the source
project table, or a MicroStrategy GUID. Missing, invalid, or ambiguous target
keys stop the workflow before import; history inserts cannot silently use `NULL`.
Datasource whole-number decimal representations such as `10.0` are normalized to
integer `10` for project keys and result statuses. Fractional and non-finite values
are rejected rather than truncated.

Set the flag to `False` to allow the existing request-ID, prefix, source-name,
and derived-name fallbacks. Strict mode requires status-to-environment mappings;
schemas without those mappings must be populated before using it.

### Missing result status mapping

Both `status_id_success` and `status_id_failed` must contain integer status IDs on
the selected `arms.t_mig_lu_tgt_env` row. They are validated before opening the
target connection or starting import. Missing environment mappings or invalid
status IDs stop the workflow; the script does not guess from status names or use
the target-project table's `status_id`.

### Import failure during `migrate()`

If connecting to the target, loading/starting the migration, polling, or required
post-import checks fail, the script:

1. Updates the selected request to the environment's `status_id_failed`.
2. Inserts a new migration-history record with that failed status, even if this
   request has no previous history. Existing history is not overwritten.
3. Inserts a request comment containing the error.
4. Re-raises the error and closes the connections.

Failure writeback uses the following request update, followed by the existing
migration-history and comment inserts:

```sql
UPDATE arms.t_mig_dim_request
SET status_id = <target_environment.status_id_failed>
WHERE request_id = <selected_request_id>;
```

Success uses `status_id_success` in both the new history record and the final
request update. These database operations are separate calls, not an atomic
transaction. Writeback errors are surfaced explicitly; partial writes are possible.

### Locked migration and project-header mismatch

The SDK normally updates the migration's target binding before starting import.
A locked package can reject that update. On that specific lock error, the script
uses the existing package ID to create a new migration instance bound to the
selected target project and environment, following the SDK's reuse API sequence.
It does not delete the original migration or retry against its old target binding.
The create request uses the SDK's `unlockOnStart=True` default.

Before starting the reused import, the script writes the new ID to `migration_obj_id`
and refreshes `migration_obj_cre_time`
to `arms.t_mig_lu_migration_obj` (or `MSTR_MIGRATION_OBJECT_TABLE`) using the selected
request's `migration_key_id`. If that write fails, import is not started. The
new instance's saved binding is verified before sending the import request with
the selected project's header. Polling and post-import checks use the new instance.
Failures propagate through normal failure writeback, including failures after reuse.
Non-lock import errors do not trigger automatic reuse.

### SQL output

Every SQL statement executed directly by this script is printed with an
`Executing SQL:` heading immediately before submission, including lookups,
the reused migration-reference update, history inserts, comments, and status
updates. Output can contain request details and error text; handle logs accordingly.

Composite migration IDs (`package:import`) are not sent as object-GUID filters to
the job monitor, which rejects them. Skipping that diagnostic means job state is
unknown, not that no jobs exist.

Import polling uses `MSTR_POST_IMPORT_TIMEOUT_SECONDS` as its timeout. Status-fetch
errors and timeouts propagate into failure writeback instead of being ignored.

### Database update failure after import

If the import succeeds but the request status update fails, the package remains imported, but the request table may still show the previous status. In that case, the import step and metadata update are no longer in sync.

## Relationship to the creation loop

[create_package_validation_loop.py](../create_package_validation_loop.py) builds
the source package and writes migration metadata into ARMS. It no longer reads
the dataset target-project fields or performs target validation/rebuild retries.
The import script consumes the existing migration object and request record and
resolves the destination independently.

That means the import script does not try to rebuild or revalidate the package. It only consumes the existing migration object reference and performs the import action.

## Practical Notes

- Requests, source project names, and migration references are retrieved through
  SQL; the import script does not execute a request report.
- The target project name is required for the target connection; result statuses
  come from the target-environment row, not the target-project row.
- The script closes connections even when an error occurs.
- The credentials are currently local script values and should be managed according to your environment policy.
