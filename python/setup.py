#!/usr/bin/env python

from setuptools import setup

# monkey-patch out the semantic versioning functionality of pbr
from functools import wraps
import pbr.packaging

@wraps(pbr.packaging.get_version)
def new_get_version(package_name, pre_version=None):
    import os
    with open(os.path.join('..', 'VERSION'), 'r') as version:
        return version.read().rstrip()
    raise Exception("please ensure this project has a VERSION file at its root")

pbr.packaging.get_version = new_get_version

setup(
    setup_requires=['pbr'],
    pbr=True,
)

# vim: set ts=4 sw=4 expandtab:
