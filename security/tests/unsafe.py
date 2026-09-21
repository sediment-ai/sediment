# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-executed Semgrep rule fixtures; each marker must produce a finding."""

import pickle
import ssl
import subprocess

import requests
import yaml


def unsafe(value):
    # ruleid: python-shell-execution
    subprocess.run(value, shell=True)
    # ruleid: python-disabled-tls
    requests.get(value, verify=False)
    # ruleid: python-unverified-ssl-context
    ssl._create_unverified_context()
    # ruleid: python-unsafe-deserialization
    pickle.loads(value)
    # ruleid: python-unsafe-yaml
    yaml.load(value, Loader=yaml.Loader)
    # ruleid: python-dynamic-eval
    eval(value)


def safe(value):
    # ok: python-shell-execution
    subprocess.run(["git", "status", value], check=True)
    # ok: python-disabled-tls
    requests.get(value, verify=True)
    # ok: python-unsafe-yaml
    yaml.safe_load(value)
