.PHONY: install clean test benchmark smoke python-matrix release-check

install:
	scripts/deploy.sh

clean:
	scripts/clean.sh

test:
	scripts/test.sh

benchmark:
	scripts/benchmark.sh

smoke:
	scripts/smoke.sh

python-matrix:
	scripts/python-matrix.sh

release-check:
	scripts/release-check.sh
