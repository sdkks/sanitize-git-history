PYTHON ?= python3

.PHONY: test

test:
	$(PYTHON) -m unittest -v tests.test_migrate_commits
