.PHONY: install clean test python-matrix release-check

install:
	scripts/deploy.sh

clean:
	scripts/clean.sh

test:
	scripts/test.sh

python-matrix:
	scripts/python-matrix.sh

release-check:
	scripts/release-check.sh
