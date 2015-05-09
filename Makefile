SHELL = /bin/bash -e

all: package

package:
	tools/py-in-venv ./python "python ./setup.py sdist --formats=gztar bdist_wheel"

publish: package
	tools/py-in-venv ./python "python ./setup.py bdist_wheel upload -r pypi"

.PHONY: package publish
