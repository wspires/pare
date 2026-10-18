#!/usr/bin/env python3
"""Fill in the Homebrew formula for a release.

    python3 packaging/homebrew/render.py 0.2.0 dist/SHA256SUMS > pare.rb

Reads the release's SHA256SUMS (as release.yml writes it) and substitutes
the version and each platform's archive checksum into pare.rb.in. Fails if
a platform the formula names has no archive in the release.
"""

import os
import re
import sys


def main() -> None:
    version, sums_path = sys.argv[1], sys.argv[2]
    sums = {}
    with open(sums_path, encoding="utf-8") as f:
        for line in f:
            digest, name = line.split()
            m = re.fullmatch(rf"pare-v{re.escape(version)}-(.+)\.(?:tar\.gz|zip)", name.lstrip("*"))
            if m:
                sums[m.group(1)] = digest
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "pare.rb.in"), encoding="utf-8") as f:
        text = f.read().replace("@VERSION@", version)

    def checksum(m: re.Match) -> str:
        target = m.group(1)
        if target not in sums:
            sys.exit(f"render.py: no archive for {target} in {sums_path}")
        return sums[target]

    sys.stdout.write(re.sub(r"@SHA256_([A-Za-z0-9_-]+)@", checksum, text))


if __name__ == "__main__":
    main()
