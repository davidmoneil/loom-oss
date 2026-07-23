-- Enable Apache AGE and create the loom graph.
-- Runs once on first container start via /docker-entrypoint-initdb.d/.
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('loom_graph');
