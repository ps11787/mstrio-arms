-- PostgreSQL arms schema: 25 table definitions, constraints and indexes.
-- Reference extract, not a full backup or ordered restore script.
-- Ownership, grants, triggers, policies, sequences and custom types are not included.
-- "arms"."t_mig_comments"
CREATE TABLE "arms"."t_mig_comments" (
    "comment_id" integer NOT NULL,
    "request_id" integer,
    "comment_text" text,
    "comment_time" timestamp without time zone,
    "comment_author" character varying(255),
    "human_flag" integer
);

-- "arms"."t_mig_dim_migration"
CREATE TABLE "arms"."t_mig_dim_migration" (
    "migration_id" integer,
    "migration_time" timestamp without time zone,
    "request_id" integer,
    "undo_pkg_name" character varying(255),
    "undo_pkg_cre_time" timestamp without time zone,
    "project_id" integer,
    "status_id" integer,
    "migration_key_id" integer
);

-- "arms"."t_mig_dim_object"
CREATE TABLE "arms"."t_mig_dim_object" (
    "name" character varying(255),
    "objecttype" character varying(255),
    "id" character varying(255),
    "folderpath" character varying(255),
    "description" character varying(255),
    "creationtime" character varying(255),
    "modificationtime" character varying(255),
    "owner" character varying(255),
    "hidden" character varying(255),
    "object_id" character varying(255),
    "project_id" integer,
    "migration_obj_id" character varying(65)
);

-- "arms"."t_mig_dim_objects"
CREATE TABLE "arms"."t_mig_dim_objects" (
    "id" character varying(32),
    "type" character varying,
    "name" character varying,
    "location" character varying,
    "date_created" timestamp with time zone,
    "date_modified" timestamp with time zone,
    "keyid" integer,
    "migration_key_id" integer
);

-- "arms"."t_mig_dim_request"
CREATE TABLE "arms"."t_mig_dim_request" (
    "request_id" integer,
    "request_time" timestamp without time zone,
    "requestor_login" character varying NOT NULL,
    "package_id" integer,
    "project_id" integer,
    "status_id" integer,
    "jira_id" character varying DEFAULT 'N/A'::character varying,
    "schema_flag" integer,
    "tgt_project_id" integer,
    "trans_flag" integer,
    "req_related" character varying,
    "vtemp_flag" integer,
    "ppm_no" character varying DEFAULT 'N/A'::character varying,
    "fr_flag" integer,
    "prop_flag" integer,
    "ext_ticket" text,
    "migration_key_id" integer,
    "validation_flag" integer
);

-- "arms"."t_mig_dim_request_old"
CREATE TABLE "arms"."t_mig_dim_request_old" (
    "request_id" integer,
    "request_time" timestamp without time zone,
    "requestor_login" character varying(100) NOT NULL,
    "package_id" money,
    "project_id" integer,
    "status_id" integer,
    "jira_id" character varying(50),
    "schema_flag" integer,
    "tgt_project_id" integer,
    "trans_flag" integer,
    "req_related" character varying(255),
    "vtemp_flag" integer,
    "ppm_no" character varying(100),
    "fr_flag" integer,
    "prop_flag" integer
);

-- "arms"."t_mig_lu_env"
CREATE TABLE "arms"."t_mig_lu_env" (
    "env_id" character varying(10),
    "env_desc" character varying(255),
    "env_order" double precision,
    "status_id" double precision,
    "env_url" character varying,
    "env_prefix" character varying
);

-- "arms"."t_mig_lu_folder"
CREATE TABLE "arms"."t_mig_lu_folder" (
    "folder_id" integer,
    "folder_path" character varying(255)
);

-- "arms"."t_mig_lu_migration_obj"
CREATE TABLE "arms"."t_mig_lu_migration_obj" (
    "migration_obj_id" character varying(65),
    "migration_obj_cre_time" timestamp without time zone,
    "migration_obj_name" character varying,
    "request_id" integer,
    "content_path" character varying,
    "migration_key_id" integer
);

-- "arms"."t_mig_lu_package"
CREATE TABLE "arms"."t_mig_lu_package" (
    "package_id" integer,
    "package_pth" character varying(255),
    "search_guid" character varying(255),
    "search_name" character varying(255),
    "search_pth" character varying(255),
    "creation_time" timestamp without time zone,
    "project_id" integer,
    "migration_obj_id" character varying(65)
);

-- "arms"."t_mig_lu_project"
CREATE TABLE "arms"."t_mig_lu_project" (
    "project_id" integer,
    "project_guid" character varying(50),
    "project_name" character varying(100),
    "project_desc" text,
    "env_id" character varying(10),
    "project_source_id" integer,
    "project_folder_loc" integer,
    "project_active_fg" integer
);

-- "arms"."t_mig_lu_project_source"
CREATE TABLE "arms"."t_mig_lu_project_source" (
    "project_source_id" integer,
    "project_source_name" character varying(255),
    "project_source_server" character varying(255),
    "project_source_alt_server" character varying(255),
    "project_source_alt_name" character varying(255)
);

-- "arms"."t_mig_lu_requestor"
CREATE TABLE "arms"."t_mig_lu_requestor" (
    "requestor_login" character varying(255),
    "requestor_name" character varying(255),
    "team_id" double precision,
    "req_email" character varying(255),
    "role_id" integer
);

-- "arms"."t_mig_lu_schema_flag"
CREATE TABLE "arms"."t_mig_lu_schema_flag" (
    "flag_id" double precision,
    "flag_desc" character varying(255)
);

-- "arms"."t_mig_lu_status"
CREATE TABLE "arms"."t_mig_lu_status" (
    "status_id" double precision,
    "status_name" character varying(255),
    "status_desc" character varying(255),
    "sort_id" double precision,
    "arch_role" integer DEFAULT 0,
    "dev_role" integer DEFAULT 0,
    "tgt_env_id" character varying
);

-- "arms"."t_mig_lu_team"
CREATE TABLE "arms"."t_mig_lu_team" (
    "team_id" double precision,
    "team_name" character varying(255)
);

-- "arms"."t_mig_lu_tgt_env"
CREATE TABLE "arms"."t_mig_lu_tgt_env" (
    "tgt_env_id" character varying,
    "env_desc" character varying,
    "env_order" integer,
    "env_url" character varying,
    "env_prefix" character varying(5),
    "status_id_success" integer,
    "status_id_failed" integer
);

-- "arms"."t_mig_lu_tgt_project"
CREATE TABLE "arms"."t_mig_lu_tgt_project" (
    "tgt_project_id" double precision,
    "tgt_project_guid" character varying(50),
    "tgt_project_name" character varying(250),
    "tgt_project_desc" character varying(255),
    "tgt_env_id" character varying(5),
    "project_source_id" double precision,
    "sort_id" double precision,
    "status_id" double precision
);

-- "arms"."t_mig_md_column_info3"
CREATE TABLE "arms"."t_mig_md_column_info3" (
    "project_name" character varying(255),
    "project_guid" character varying(255),
    "prefix_name" character varying(255),
    "table_guid" character varying(255),
    "table_name" character varying(255),
    "column_guid" character varying(255),
    "column_name" character varying(255),
    "create_time" character varying(255),
    "mod_time" character varying(255),
    "version_id" character varying(255),
    "column_owner" character varying(255)
);

-- "arms"."t_mig_rel_obj_package"
CREATE TABLE "arms"."t_mig_rel_obj_package" (
    "package_id" double precision,
    "obj_guid" character varying(255),
    "rel_id" double precision,
    "object_id" character varying(255)
);

-- "arms"."t_mig_stg_dim_object"
CREATE TABLE "arms"."t_mig_stg_dim_object" (
    "id" character varying(32),
    "type" character varying,
    "name" character varying,
    "location" character varying,
    "date_created" timestamp with time zone,
    "date_modified" timestamp with time zone,
    "keyid" integer
);

-- "arms"."v_cal_calendarday"
CREATE TABLE "arms"."v_cal_calendarday" (
    "dat_calendar" date
);

-- "arms"."v_mig_lu_jira2"
CREATE TABLE "arms"."v_mig_lu_jira2" (
    "jira_id" character varying(255)
);

-- "arms"."v_mig_lu_object_type"
CREATE TABLE "arms"."v_mig_lu_object_type" (
    "objecttype" character varying(255)
);

-- "arms"."v_mig_lu_ppm2"
CREATE TABLE "arms"."v_mig_lu_ppm2" (
    "ppm_no" character varying(255)
);


-- Constraints

-- Non-constraint indexes
