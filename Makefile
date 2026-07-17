PYTHON := .venv/bin/python

.PHONY: setup check-venv test lint test-llm test-email telegram-login run digest-now cleanup security-check healthcheck

setup:
	python3.12 -m venv .venv
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e ".[dev]"

check-venv:
	@test -x $(PYTHON) || (echo "Virtualenv missing. Run make setup."; exit 1)

test: check-venv
	$(PYTHON) -m pytest

lint: check-venv
	$(PYTHON) -m ruff check .

test-llm: check-venv
	$(PYTHON) -m app.cli.test_llm

test-email: check-venv
	$(PYTHON) -m app.cli.test_email

telegram-login: check-venv
	$(PYTHON) -m app.cli.telegram_login

run: check-venv
	$(PYTHON) -m app.cli.run

digest-now: check-venv
	$(PYTHON) -m app.cli.digest_now

cleanup: check-venv
	$(PYTHON) -m app.cli.cleanup

security-check: check-venv
	$(PYTHON) -m app.cli.security_check

healthcheck: check-venv
	$(PYTHON) -m app.cli.healthcheck
