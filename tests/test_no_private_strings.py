"""Nothing private ships: no names, addresses, hosts or paths from the machine this was built on, and nothing
that looks like a key. The repository's own URL is the one allowed mention of its owner's account."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = re.compile(r"senecta|denpod|marcos|junca|mauro|taleva|mj924|34\.21\.121\.199|34\.86\.164\.31|"
                     r"34\.175\.112\.175|C:\\Users|Users/marco", re.I)
ALLOWED = re.compile(r"github\.com/marcosjunca06/jlc-agent", re.I)
KEYS = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|"
                  r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = [ROOT / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):  # not a checkout (an sdist): every file there ships
        files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [p for p in files if p.is_file() and p.resolve() != Path(__file__).resolve()]


class NothingPrivateTest(unittest.TestCase):
    def test_no_private_strings_or_keys(self):
        found = []
        for path in tracked():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if PRIVATE.search(ALLOWED.sub("", line)) or KEYS.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
