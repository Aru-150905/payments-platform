.PHONY: up ui down topics migrate seed api worker relay logs demo

up:
	docker compose up -d

ui:
	docker compose --profile ui up -d kafka-ui

down:
	docker compose down -v

topics:
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
		--create --if-not-exists --topic payments.payment.v1 --partitions 3 --replication-factor 1
	docker exec pp-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
		--create --if-not-exists --topic payments.dlq.v1 --partitions 1 --replication-factor 1
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
