# This purpose of this makefile is to inject custom setup into your test builds
.PHONY: custom-tests
custom-tests: #Inject any setup required before running unit tests.
custom-tests:
	@echo "unit-tests : no custom setup required."

.PHONY: custom-integration-tests
custom-integration-tests: #Inject any setup required before running integration tests.
custom-integration-tests:
	@echo "integration-tests : no custom setup required."
