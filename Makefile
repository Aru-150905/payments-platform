.PHONY: up ui observability down topics migrate seed api worker relay logs demo \
	rebuild-read-model reconcile retention-purge load-test

up:
	docker compose up -d

ui:
	docker compose --profile ui up -d kafka-ui

# Off by default — see docker-compose.yml's comment on the "observability"
# profile for why (8GB dev machine). Prometheus: http://localhost:9090,
# Grafana (anonymous admin, no login): http://localhost:3000.
observability:
	docker compose --profile observability up -d prometheus grafana

down:
	docker compose down -v

# retention.ms is set explicitly (7 days) rather than left at the broker's
# default so it's a documented number, not an implicit one — see
# app/services/retention.py, which justifies processed_events' own retention
# window against this exact figure.
topics:
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
		--create --if-not-exists --topic payments.payment.v1 --partitions 3 --replication-factor 1 \
		--config retention.ms=604800000
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
		--create --if-not-exists --topic payments.dlq.v1 --partitions 1 --replication-factor 1 \
		--config retention.ms=604800000
	# 3 partitions, keyed by instrument id (app/events/topics.py) — several
	# instruments trade in parallel across partitions; all events for ONE
	# instrument land on the same partition and stay ordered.
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
		--create --if-not-exists --topic trading.order.v1 --partitions 3 --replication-factor 1 \
		--config retention.ms=604800000
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list

migrate:
	alembic upgrade head

seed:
	python -m scripts.seed

api:
	uvicorn app.main:app --reload --port 8000

relay:
	python -m app.events.relay

worker:
	python -m app.events.consumer

logs:
	docker compose logs -f kafka

demo:
	bash scripts/demo.sh

# Do not run this while `make worker` is also running against the same
# topic — see ADR 0006's consequences.
rebuild-read-model:
	python -m scripts.rebuild_read_model

reconcile:
	python -m scripts.reconcile

retention-purge:
	python -m scripts.retention

# Needs the API running (`make api`) and k6 installed separately — it's a
# standalone Go binary, not a Python dependency. See k6/m5_load_test.js's
# header comment for what it exercises and why.
load-test:
	k6 run k6/m5_load_test.js
