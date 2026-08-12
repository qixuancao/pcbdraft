.PHONY: install test benchmark smoke compatibility release-check

install:
	scripts/deploy.sh

test:
	scripts/test.sh

benchmark:
	scripts/benchmark.sh

smoke:
	scripts/smoke.sh

compatibility:
	scripts/compatibility.sh

release-check:
	scripts/release-check.sh
