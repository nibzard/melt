# RFC 8785 conformance fixtures

These files are the reference test vectors of the JSON
Canonicalization Scheme project at
<https://github.com/cyberphone/json-canonicalization>, `testdata`
directory. RFC 8785 appendix H points at them. They are Apache
License 2.0 works of the WebPKI.org project, copied unmodified.

- `input-*.json`: the value to canonicalize.
- `expected-*.json`: the canonical serialization, exactly as the
  reference suite expects it.

`tests/contract/test_canonical.py` runs every pair against
`melt.schema.canonical.canonicalize`. The ES6 number edge cases from
the same project's `verify-numbers.py` run against
`serialize_number`.
