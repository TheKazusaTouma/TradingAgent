"""Course governance tooling: schema, type, dependency, security, AI-use and eval checks.

These modules are run by CI (see .github/workflows/ci.yml) and by contributors
locally. They are not part of the installed package; nothing here imports the
runtime under test except the fixed offline evaluation replay, and that only
through the existing test fixtures.
"""
