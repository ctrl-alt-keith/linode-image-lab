PYTHON ?= python3
export PYTHONPATH := src

.PHONY: check test security-check

check: security-check test

test:
	$(PYTHON) -m unittest discover -s tests/unit -p 'test_*.py'

security-check:
	$(PYTHON) -m linode_image_lab.validation .
