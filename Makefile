.PHONY: setup test lint test-llm test-email telegram-login run digest-now cleanup

setup:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install -U pip
	.venv/bin/python -m pip install -e ".[dev]"

test:
	python -m pytest

lint:
	python -m ruff check .

test-llm:
	python -m app.cli.test_llm

test-email:
	python -m app.cli.test_email

telegram-login:
	python -m app.cli.telegram_login

run:
	python -m app.cli.run

digest-now:
	python -m app.cli.digest_now

cleanup:
	python -m app.cli.cleanup

