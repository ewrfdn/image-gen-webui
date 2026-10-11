-- Design draft, PostgreSQL. No model/LoRA catalogue tables and no workspace entities.
CREATE TABLE backend_nodes (
  id varchar(64) PRIMARY KEY,
  name varchar(128) NOT NULL,
  base_url text NOT NULL UNIQUE,
  inference_secret_ciphertext text NOT NULL,
  admin_secret_ciphertext text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE api_keys (
  id uuid PRIMARY KEY,
  name varchar(128) NOT NULL,
  prefix varchar(32) NOT NULL,
  key_hash varchar(128) NOT NULL UNIQUE,
  enabled boolean NOT NULL DEFAULT true,
  revoked_at timestamptz,
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE system_settings (
  id integer PRIMARY KEY CHECK (id = 1),
  max_count integer NOT NULL DEFAULT 100 CHECK (max_count > 0),
  catalog_refresh_seconds integer NOT NULL DEFAULT 5 CHECK (catalog_refresh_seconds > 0),
  node_stale_seconds integer NOT NULL DEFAULT 15,
  CHECK (node_stale_seconds >= catalog_refresh_seconds),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO system_settings(id) VALUES (1);
CREATE TABLE media_tasks (
  id uuid PRIMARY KEY,
  type varchar(16) NOT NULL CHECK (type IN ('image', 'video')),
  status varchar(16) NOT NULL DEFAULT 'pending' CHECK (status IN
    ('pending', 'processing', 'loading', 'submitted', 'running', 'persisting', 'succeeded', 'failed', 'unknown')),
  source varchar(16) NOT NULL CHECK (source IN ('ui', 'api')),
  api_key_id uuid REFERENCES api_keys(id),
  owner_subject varchar(128) NOT NULL,
  model_id varchar(128) NOT NULL,
  model_revision varchar(128) NOT NULL,
  model_artifact_sha256 char(64) NOT NULL,
  lora_snapshot jsonb NOT NULL DEFAULT '[]',
  capability_snapshot jsonb NOT NULL,
  request_body jsonb NOT NULL,
  node_id varchar(64) REFERENCES backend_nodes(id),
  current_attempt_id uuid,
  external_task_id varchar(128),
  wait_reason varchar(128),
  error text,
  raw_result jsonb,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_expires_at timestamptz,
  submitted_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_media_tasks_status_type_created ON media_tasks(status, type, created_at, id);
CREATE INDEX ix_media_tasks_owner_created ON media_tasks(owner_subject, created_at);
CREATE INDEX ix_media_tasks_node ON media_tasks(node_id);
CREATE TABLE task_attempts (
  id uuid PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES media_tasks(id),
  node_id varchar(64) NOT NULL REFERENCES backend_nodes(id),
  attempt_number integer NOT NULL,
  status varchar(32) NOT NULL,
  submitted_at timestamptz NOT NULL,
  finished_at timestamptz,
  external_task_id varchar(128),
  error text,
  execution_metadata jsonb,
  UNIQUE(task_id, attempt_number),
  UNIQUE(task_id, id)
);
ALTER TABLE media_tasks ADD CONSTRAINT fk_current_attempt_belongs_to_task
  FOREIGN KEY (id, current_attempt_id) REFERENCES task_attempts(task_id, id);
CREATE TABLE media_files (
  id uuid PRIMARY KEY,
  task_id uuid REFERENCES media_tasks(id), -- NULL for uploaded references before submission
  attempt_id uuid,
  owner_subject varchar(128) NOT NULL,
  role varchar(16) NOT NULL CHECK (role IN ('reference', 'output')),
  position integer NOT NULL DEFAULT 0 CHECK (position >= 0),
  storage_backend varchar(16) NOT NULL CHECK (storage_backend IN ('local', 'object')),
  storage_key text NOT NULL,
  mime_type varchar(128) NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  sha256 char(64) NOT NULL,
  width integer,
  height integer,
  duration_seconds numeric,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (task_id, attempt_id) REFERENCES task_attempts(task_id, id),
  CHECK (role <> 'output' OR (task_id IS NOT NULL AND attempt_id IS NOT NULL)),
  UNIQUE(storage_backend, storage_key),
  UNIQUE(task_id, attempt_id, role, position)
);
CREATE INDEX ix_media_files_task ON media_files(task_id);

-- Submission: hold singleton row lock, count pending/processing/loading,
-- reject if count >= max_count, otherwise INSERT task and COMMIT.
-- Network calls must never run within these transactions.

-- Limited claim example (bind :task_type and :batch_size in implementation):
-- WITH candidates AS (
--   SELECT id FROM media_tasks WHERE status = 'pending' AND type = :task_type
--   ORDER BY created_at, id LIMIT :batch_size FOR UPDATE SKIP LOCKED
-- ) UPDATE media_tasks AS t SET status = 'processing', updated_at = now(),
--   lease_expires_at = now() + interval '30 seconds'
-- FROM candidates AS c WHERE t.id = c.id RETURNING t.*;
