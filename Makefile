.PHONY: docs test all-tests integration-toolchain integration-test dev-build docs-serve

# Absolute path to the driver-integration toolchain, needed on LD_LIBRARY_PATH
# so the pyodbc wheel resolves our unixODBC before Python starts.
INTEGRATION_DIR := tests/integration
INTEGRATION_TOOLCHAIN := $(abspath $(INTEGRATION_DIR)/.toolchain)

test:
	python -m unittest discover -s tests

# Build/download the ODBC + JDBC driver toolchain the integration tests need.
# Add --with-jdbc-tool as an argument here to also fetch the optional SQL
# Workbench/J client (make integration-toolchain ARGS=--with-jdbc-tool).
integration-toolchain:
	$(INTEGRATION_DIR)/setup_toolchain.sh $(ARGS)

# Run the driver integration tests. The ODBC environment is set before Python
# starts (the dynamic loader reads LD_LIBRARY_PATH only at startup). Tests whose
# toolchain piece is missing skip themselves, so this stays green on a machine
# where integration-toolchain has not been run.
integration-test:
	LD_LIBRARY_PATH=$(INTEGRATION_TOOLCHAIN)/unixodbc/lib \
	ODBCSYSINI=$(INTEGRATION_TOOLCHAIN)/etc \
	ODBCINI=$(INTEGRATION_TOOLCHAIN)/etc/odbc.ini \
	python -m unittest discover -s $(INTEGRATION_DIR) -t $(INTEGRATION_DIR)

all-tests:
	maturin develop
	python3 -m unittest discover -s tests
	python3 -m unittest discover -s test_concurrency
	cd teleduck && pip install -e . && python3 -m unittest discover -s tests
	$(MAKE) integration-test

dev-build:
	maturin build --profile=fast -i python3

docs:
	mkdocs build

docs-serve:
	mkdocs serve -a 0.0.0.0:8000
