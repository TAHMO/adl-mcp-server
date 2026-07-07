-- ============================================================================
-- adl-mcp-server test fixture — a MINIMAL but GROUNDED mini-ADL.
--
-- Table and column names mirror ADL's real data model so the MCP is smoke-tested
-- against ADL's actual shape, not a guess. This is a subset for testing the read
-- surface — NOT the full ADL schema.
--
-- Provenance:
--   ADL Core Models Reference
--     https://adl-tool.readthedocs.io/en/latest/developer_guide/models_reference.html
--   adl/src/adl/core/models.py  (github.com/wmo-raf/adl)
--   ObservationRecord(TimescaleModel): table core_observationrecord (hypertable),
--     time/value, FK station_id/connection_id/parameter_id,
--     unique (time, station, connection, parameter); hourly cagg view obs_agg_1h_v.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis CASCADE;   -- Station.location is a PostGIS PointField

CREATE TABLE core_network (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    type        text,
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE core_unit (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    symbol      text NOT NULL,
    description text
);

CREATE TABLE core_dataparameter (
    id                 bigserial PRIMARY KEY,
    name               text NOT NULL,
    description        text,
    unit_id            bigint REFERENCES core_unit(id),
    aggregation_method text,
    is_coded           boolean DEFAULT false,
    wmo_code_table     text
);

CREATE TABLE core_networkconnection (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    network_id  bigint REFERENCES core_network(id)
);

CREATE TABLE core_station (
    id                       bigserial PRIMARY KEY,
    station_id               text NOT NULL,                 -- human-readable code
    name                     text NOT NULL,
    network_id               bigint REFERENCES core_network(id),
    location                 geometry(Point, 4326),         -- PostGIS PointField (lon/lat)
    wsi_series               int,
    wsi_issuer               int,
    wsi_issue_number         int,
    wsi_local                text,
    wmo_block_number         int,
    wmo_station_number       int,
    station_height_above_msl double precision
);

-- TimescaleDB hypertable: no serial PK (the unique key includes the time column).
CREATE TABLE core_observationrecord (
    id            bigserial,
    "time"        timestamptz NOT NULL,                     -- TimescaleModel time (UTC)
    created_at    timestamptz DEFAULT now(),
    modified_at   timestamptz DEFAULT now(),
    station_id    bigint NOT NULL REFERENCES core_station(id),
    connection_id bigint NOT NULL REFERENCES core_networkconnection(id),
    parameter_id  bigint NOT NULL REFERENCES core_dataparameter(id),
    value         double precision,
    is_daily      boolean DEFAULT false,
    qc_status     smallint DEFAULT 0,
    qc_version    int DEFAULT 1,
    UNIQUE ("time", station_id, connection_id, parameter_id)
);
SELECT create_hypertable('core_observationrecord', 'time');

-- reference data ------------------------------------------------------------
INSERT INTO core_network (name, type) VALUES ('ADCON Automatic Weather Stations', 'aws');
INSERT INTO core_networkconnection (name, network_id) VALUES ('adcon-primary', 1);
INSERT INTO core_unit (name, symbol) VALUES
    ('degree Celsius', 'degC'), ('percent', '%'), ('millimetre', 'mm');
INSERT INTO core_dataparameter (name, unit_id, aggregation_method) VALUES
    ('air_temperature',   1, 'instant'),
    ('relative_humidity', 2, 'instant'),
    ('precipitation',     3, 'sum');

INSERT INTO core_station
    (station_id, name, network_id, location, wmo_block_number, wmo_station_number, station_height_above_msl)
VALUES
    ('ET-ADD-001', 'Addis Ababa Bole', 1, ST_SetSRID(ST_MakePoint(38.80,  8.98), 4326), 63, 450, 2334),
    ('KE-NBO-001', 'Nairobi JKIA',     1, ST_SetSRID(ST_MakePoint(36.93, -1.32), 4326), 63, 740, 1624),
    ('NG-LOS-001', 'Lagos Ikeja',      1, ST_SetSRID(ST_MakePoint( 3.33,  6.58), 4326), 65, 201,   39);

-- 30 days of hourly obs x 3 stations x 3 parameters
INSERT INTO core_observationrecord ("time", station_id, connection_id, parameter_id, value)
SELECT ts, s.id, 1, p.id, round((random() * 30 + 10)::numeric, 2)::double precision
FROM   generate_series(now() - interval '30 days', now(), interval '1 hour') AS ts
CROSS JOIN core_station s
CROSS JOIN core_dataparameter p;

-- hourly continuous aggregate exposed (in real ADL) as the view obs_agg_1h_v
CREATE MATERIALIZED VIEW obs_agg_1h_v
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', "time") AS bucket,
       station_id, connection_id, parameter_id,
       min(value) AS min_value, max(value) AS max_value,
       avg(value) AS avg_value, sum(value) AS sum_value,
       count(*)   AS records_count
FROM core_observationrecord
GROUP BY bucket, station_id, connection_id, parameter_id
WITH NO DATA;
