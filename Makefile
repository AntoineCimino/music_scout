.PHONY: install test run

install:
	pip install -r requirements.txt

test:
	pytest tests/ -v

run:
	python -m src.cli --help
