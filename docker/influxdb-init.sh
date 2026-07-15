#!/bin/bash
# First-boot init inside the influxdb container (docker-entrypoint-initdb.d).
# Setup already created alarm_engine_metrics; add the infinite-retention rollup.
set -e
influx bucket create \
  --name alarm_engine_metrics_rollup \
  --org "$DOCKER_INFLUXDB_INIT_ORG" \
  || echo "rollup bucket already exists"
