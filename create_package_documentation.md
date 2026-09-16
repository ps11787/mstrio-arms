# Documentation for create_package.py

## Purpose

This script automates the creation of a MicroStrategy migration package for a specific request. It reads request metadata from a MicroStrategy report, resolves the target folder, collects related objects, creates a package, stores the package and object information in ARMS tables, triggers validation against the target project, and automatically retries the process when validation reports missing objects.

This is an operational automation script for a specific migration workflow, not a generic package generator.

---

## Main workflow

The script performs the following steps in order:

1. Connects to the MicroStrategy environment.
2. Reads a request report to obtain migration metadata.
3. Normalizes the migration folder path.
4. Resolves the folder ID used for the migration.
5. Reads shortcut objects from that folder.
6. Resolves each shortcut to its real target object.
7. Builds the migration package configuration.
8. Deletes any old migration package if the request already has one.
9. Creates a new migration package.
10. Stores package/object details in ARMS tables.
11. Updates request status and package references.
12. Validates the package against the target project.
13. Automatically adds missing objects and retries validation when needed.
14. Updates the final validation flag after success or failure.

---

## Required inputs and configuration

The script uses fixed values directly in the code, including:

- Base URL
- Username and password
- Project ID
- Datasource instance ID
- Report ID
- Target project name
- Source project name

These values are not supplied through arguments or environment variables; they are embedded in the script.

The script expects a report structure similar to this:

- Target Project@NAME
- Target Project@ID
- Status@Name
- Status@ID
- Team
- Requestor
- Request@ID
- Request@TimeStamp
- Migration Obj@ID
- Migration Obj@Name
- Migration Obj@Content Path
- Migration Obj@GUID
- Project@ID
- Project@Name

If these fields are missing or empty, the script may fail at runtime.

---

## Connection setup

The script creates two connections:

1. A main MicroStrategy connection using `Connection(...)`
2. A datasource instance connection using `DatasourceInstance(...)`

This is used both for:

- reading the request report and project metadata
- executing SQL against the ARMS datasource

The script then loads the report with:

```python
request_list = Report(connection=myConn, id='A62CDD7147492E20EE83C5841B7C9183')
```

If the report cannot be read, the rest of the script cannot continue.

---

## Folder path normalization

The helper function `_normalize_mstr_folder_path(project_name, migration_path)` handles the migration folder path.

### Behavior

- If the path is empty, it raises an error.
- If the path starts with `/`, it is considered an absolute MicroStrategy folder path.
- If the path is relative, it is converted to a project-relative path:

```text
relative path: Reports/Finance
result:        /ProjectName/Reports/Finance
```

- Backslashes are replaced with forward slashes.

### Why this matters

MicroStrategy usually expects folder paths in Unix-style absolute format. Without normalization, folder resolution may fail or point to the wrong location.

---

## Folder resolution

The helper `_resolve_folder_id(connection, folder_path, project_name)` tries to resolve the folder path to its internal ID.

### Logic

1. Try `get_folder_id_from_path(connection, path=folder_path)`.
2. If that fails, iterate through all folders in the project using `list_folders(...)`.
3. Match the normalized folder path against the folder path returned by MicroStrategy.
4. If no match is found, raise an error.

### If this fails

The script stops because the code later uses the resolved folder ID to read the objects that should be migrated.

---

## Reading migration objects

The script reads all content from the resolved folder:

```python
shortcuts4Migration = Folder(myConn, id=_resolve_folder_id(...)).get_contents()
```

Then it iterates through each object and resolves its dependency target:

```python
target = obj.list_dependencies()[0]
```

This assumes that each shortcut resolves to exactly one target object.

### Object properties collected

For each resolved object, the script stores:

- ID
- Type
- Name
- Date modified

And for the more detailed ARMS insert, it also adds:

- Location
- Date created
- Date modified

These entries are later used to generate the insert statements for ARMS tables.

---

## Creating the package

The script defines package settings with `PackageSettings(...)`.

These settings determine how the migration package behaves, such as:

- whether to reuse existing objects
- how schema updates should be handled
- how ACLs are applied to new or replaced objects

Then it builds the package configuration:

```python
myPackageConfig = Migration.build_package_config(
    connection=myConn,
    content=objects4Migration,
    package_settings=myPackageSettings,
)
```

This is the content used to create the migration package in MicroStrategy.

---

## Deleting an old migration

Before creating a new package, the script checks the request record:

```python
if df['Migration Obj@GUID'][0] != "":
    current_migration = Migration(myConn, id=df['Migration Obj@GUID'][0])
    current_migration.delete(force=True)
```

### What happens if this is true

- A previous migration package for the same request exists.
- It is deleted before creating the new package.

### What happens if this is false

- No old package is deleted.
- A new package is created normally.

This avoids duplicate or stale migration objects for the same request.

---

## New package creation

The script creates a package name like this:

```text
Req_<RequestID>_<Migration Obj Name>_<timestamp>
```

Then it calls:

```python
Migration.create_object_migration(
    myConn,
    toc_view=myPackageConfig,
    name=package_name,
    project_name=df['Project@Name'][0],
)
```

This creates the migration object in the source project.

---

## SQL updates in ARMS tables

The script writes package-related records into ARMS tables using `DatasourceInstance.execute_query(...)`.

### 1) Clear old objects for the request

```sql
delete from arms.t_mig_dim_objects
where migration_key_id = (
    select migration_key_id from arms.t_mig_dim_request where request_id = ...
);
```

This removes stale object records for the same request before inserting the current set.

### 2) Insert current migration objects

The helper `_build_object_insert_queries(...)` creates SQL insert statements for every object in the migration package.

Each insert stores:

- object ID
- type
- name
- location
- created date
- modified date
- migration key ID

### 3) Insert migration comments

The script inserts entries into `arms.t_mig_comments` such as:

- Package created
- Validation failed
- Validation successful
- Object added to package

### 4) Update request status and migration mapping

SQL updates are executed to:

- set request status to `2` (Package Created)
- write the migration object ID back to `arms.t_mig_lu_migration_obj`
- set validation flag after validation completes

---

## Validation loop

After package creation, the script runs a validation loop against the target environment.

### Target environment creation

```python
targetENV = Connection(base_url, mstr_username, mstr_password, project_name=target_project_name)
```

This establishes a connection to the target project environment.

### Validation trigger

```python
validation_trigger_result = current_migration.trigger_validation(
    target_env=targetENV,
    target_project_name=target_project_name,
)
```

### Polling logic

The script loops until the validation reaches one of these terminal states:

- `ValidationStatus.VALIDATED`
- `ValidationStatus.VALIDATION_FAILED`

It polls every 5 seconds:

```python
sleep(5)
```

During the loop it prints:

- validation status
- progress
- message
- creation date
- last update date

---

## What happens if validation fails?

If the validation status is `VALIDATION_FAILED`, the script does the following:

1. Writes a failure comment to `arms.t_mig_comments`.
2. Tries to parse the validation message to identify the missing object.
3. If the missing object can be detected, it adds that object to the migration package.
4. Rebuilds the package configuration.
5. Deletes the current migration package.
6. Creates a new package with the additional object.
7. Re-inserts the ARMS object records.
8. Re-runs validation.

This is the main recovery mechanism of the script.

---

## How missing objects are identified

The function `_parse_missing_object(validation_message)` tries to extract an object ID and object type from the validation error text.

It looks for patterns like:

```text
Object: <Type id=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx> is not found
```

### Supported logic

- First tries a modern pattern matching form.
- If that fails, falls back to a legacy parser.
- Converts the discovered type into a MicroStrategy `ObjectTypes` enum when possible.

### If the message cannot be parsed

The script does not know which object is missing, so it does not add anything automatically.

It just logs that validation failed and retries without recovery.

---

## What happens when a missing object is added?

When a missing object is found, the script does this:

1. Creates an `Object(...)` instance with the object ID and type.
2. Appends it to `objects4Migration`.
3. Writes a comment such as:
   - `Added object to migration package: <name> (ID: ..., type: ...)`
4. Appends the detailed object info to `objects4Migration_properties`.
5. Marks the object ID in `added_missing_object_ids` so it is not added again.
6. Rebuilds the package configuration.
7. Deletes the current migration package and creates a retry package.
8. Rewrites the ARMS object entries for the new package.

This creates a retry loop that gradually expands the package until validation passes or the failure becomes non-recoverable.

---

## Avoiding duplicate additions

The set `added_missing_object_ids` prevents the script from repeatedly adding the same missing object.

### If the same object is detected again

The script logs:

```text
Object <id> is already in the package; retrying validation.
```

Then it continues without re-adding it.

This prevents infinite loops caused by the same missing object being repeatedly reported.

---

## Validation result handling

At the end, the script reads the final validation result and sets the request validation flag:

```python
if validation_result.status == ValidationStatus.VALIDATED:
    validation_flag = 1
else:
    validation_flag = 0
```

Then it executes:

```sql
UPDATE arms.t_mig_dim_request
SET validation_flag = ...
WHERE request_id = ...;
```

### Final outcomes

- If valid: validation flag is set to `1`
- If invalid: validation flag is set to `0`
- A comment is written in both cases for traceability

---

## Typical success scenario

The script succeeds when all of the following are true:

- the MicroStrategy connection works
- the report contains valid request data
- the folder path resolves correctly
- the folder contains valid shortcut objects
- the dependencies can be resolved
- the package can be created
- validation passes against the target project

In that case, the script ends by marking the request as validated and recording the final status.

---

## Typical failure scenarios

### Scenario 1: Folder path is invalid

Result:

- path normalization or folder resolution fails
- the script stops before package creation

### Scenario 2: Missing shortcut dependencies

Result:

- `obj.list_dependencies()[0]` may fail
- script execution stops at that point

### Scenario 3: Validation reports missing objects

Result:

- the script automatically adds them
- retry validation loop runs

### Scenario 4: Validation fails but message is not parseable

Result:

- the script cannot identify the missing object
- it retries validation without recovery logic

### Scenario 5: Existing migration exists

Result:

- the old migration is deleted before creating the new package
- no stale package remains

---

## Important operational notes

- The script uses hardcoded environment values.
- It depends on specific ARMS database tables and column naming conventions.
- It assumes dedicated request metadata is present in the report.
- It modifies the system state by creating packages, updating SQL tables, and writing comments.
- It is better suited for controlled, scheduled migration automation than for interactive manual use.

---

## Summary

This script is a complete migration package lifecycle automation.

It does not simply create a package once. It:

- collects objects from a folder
- builds package configuration
- creates package records
- updates ARMS workflow tables
- validates against the target environment
- adds missing objects automatically when validation fails
- re-runs validation until the package is accepted or the failure becomes non-recoverable

That makes it a self-healing migration workflow for a specific MicroStrategy request flow.
