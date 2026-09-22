#!/bin/bash
# The test suite runs against the real forms, not hand-built stand-ins. These
# are US government works and are in the public domain.
set -euo pipefail
cd "$(dirname "$0")"
curl -sSL -A "Mozilla/5.0" -o fw9.pdf  https://www.irs.gov/pub/irs-pdf/fw9.pdf
curl -sSL -A "Mozilla/5.0" -o fw4.pdf  https://www.irs.gov/pub/irs-pdf/fw4.pdf
curl -sSL -A "Mozilla/5.0" -o i-9.pdf  https://www.uscis.gov/sites/default/files/document/forms/i-9.pdf
ls -la ./*.pdf
