# Import Package Script

This document explains the workflow of [import_package.py](../import_package.py) and how it relates to [create_package_validation_loop.py](../create_package_validation_loop.py).

## Purpose

The import script is the downstream step after the package creation and validation flow. The previous script builds a migration package, validates it against the target project, and records the migration object information in the request tables. The import script then takes the already created migration object and imports it into the target project.

In short:

1. [create_package_validation_loop.py](../create_package_validation_loop.py) creates the package.
2. It validates the package and updates the request metadata.
3. [import_package.py](../import_package.py) loads the existing migration object and executes the import into the target project.

## Workflow

The script follows these steps:

1. Connect to the source environment through `Connection`.
2. Load the request report and convert it to a dataframe.
3. Read the first row as the active request context.
4. Extract the request ID, source project name, target project name, and `Migration Obj@GUID`.
5. Connect to the target environment using the target project name.
6. Load the migration object with `Migration(my_conn, id=migration_obj_guid)`.
7. Call `migrate()` to import the package into the target project.
8. Resolve the target project `status_id` from the ARMS lookup table.
9. Update `arms.t_mig_dim_request.status_id` for the current request.
10. Close both connections in the `finally` block.

## Expected Behavior

### Successful import

If the migration object exists and the target environment is reachable, the script imports the package and then updates the request status to the target project's `status_id`.

### Empty request report

If the request report returns no rows, the script raises `ValueError` and stops. This means there is no request context to import.

### Missing migration object GUID

If `Migration Obj@GUID` is empty, the script raises `ValueError`. The import cannot start because there is no package identifier.

### Missing target project name

If the request does not contain `Target Project@NAME`, the script raises `ValueError`. The target environment cannot be opened without that value.

### Missing target status mapping

If the ARMS lookup table does not contain a `status_id` for the target project, `_get_target_project_status_id()` raises `ValueError`. The import may already be complete, but the request table cannot be updated with the target status.

### Import failure during `migrate()`

If `Migration.migrate()` fails, the exception is propagated. The `finally` block still closes both connections, and the request status is not updated by this script.

### Database update failure after import

If the import succeeds but the request status update fails, the package remains imported, but the request table may still show the previous status. In that case, the import step and metadata update are no longer in sync.

## Relationship to the creation loop

[create_package_validation_loop.py](../create_package_validation_loop.py) is responsible for building the package, retrying validation when missing objects are discovered, and writing the migration metadata into the ARMS tables. The import script assumes that this earlier workflow already produced a valid migration object and a matching request record.

That means the import script does not try to rebuild or revalidate the package. It only consumes the existing migration object reference and performs the import action.

## Practical Notes

- The script reads only the first row from the request report.
- The request report is treated as the source of truth for which migration object to import.
- The target project name is required both for the target connection and for the ARMS status lookup.
- The script closes connections even when an error occurs.
- The credentials are currently local script values and should be managed according to your environment policy.
