"""The catalogue of JLC's web API: every call the site's own apps make, found in their JavaScript.

`jlc-agent endpoints [words]` searches it. It is built on your machine, not shipped: a refresh
downloads the current front-end bundles of the four apps an account uses (the main site, the user
centre with the orders, the quote page, the assembly wizard), the same files any browser loads, and
`catalog.cjs` resolves each call's service prefix through the import it came from. JLC ships new front
ends often, so refresh when a call that used to work answers 404, or before relying on a path found
here for the first time.

Each entry carries the HTTP method when the call site shows it, up to three minified snippets of how
JLC's own pages call it (their arguments), and `kind` from the gate: read (free), write (needs
write=True) or blocked (pays or orders; never called).
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

from .app import DATA, JlcError, classify

APPS = {
    "main": "https://jlcpcb.com/",
    "user-center": "https://jlcpcb.com/user-center/orders",
    "quote": "https://cart.jlcpcb.com/quote",
    "assembly": "https://cart.jlcpcb.com/smt-order/",
}
CATALOG = DATA / "endpoints.json"
BUNDLES = DATA / "bundles"
EXTRACTOR = Path(__file__).with_name("catalog.cjs")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as response:
        return response.read()


def bundle_urls(page_url: str) -> list[str]:
    """Every chunk of the webpack app behind `page_url`: the scripts the page names, plus every chunk in
    its runtime's map (the pages that call most of the API load lazily)."""
    origin = re.match(r"https://[^/]+", page_url).group(0)
    html = _get(page_url).decode("utf-8", "replace")
    scripts = [origin + s for s in re.findall(r'src="(/[^"]+\.js)"', html)]
    runtime = next((s for s in scripts if re.search(r"/[0-9a-f]{7}\.js$", s)), None)
    urls = [s for s in scripts if re.search(r"/js/[0-9a-f]{20}\.js$", s)]
    if runtime:
        text = _get(runtime).decode("utf-8", "replace")
        folder = runtime.rsplit("/", 1)[0] + "/js/"
        for chunk_map in re.findall(r'\{((?:\d+:"[0-9a-f]{20}",?)+)\}', text):
            urls += [folder + h + ".js" for h in re.findall(r'"([0-9a-f]{20})"', chunk_map)]
    return sorted(set(urls))


def refresh(log: Callable[[str], None] = print) -> list[dict]:
    """Download the four apps' bundles and rebuild the catalogue from them (about 50 MB, a minute)."""
    if shutil.which("node") is None:
        raise JlcError("building the endpoint catalogue needs Node.js (`node` on PATH) to read JLC's bundles")
    shutil.rmtree(BUNDLES, ignore_errors=True)
    folders = []
    for app, page in APPS.items():
        urls = bundle_urls(page)
        folder = BUNDLES / app
        folder.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            for url, data in zip(urls, pool.map(_get, urls)):
                (folder / url.rsplit("/", 1)[1]).write_bytes(data)
        log(f"{app}: {len(urls)} bundle files")
        folders.append(f"{app}={folder}")
    result = subprocess.run(["node", str(EXTRACTOR), *folders], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise JlcError(f"reading the bundles failed: {result.stderr.strip()[:300]}")
    entries = json.loads(result.stdout)
    for entry in entries:
        entry["kind"] = classify(entry["path"])
    CATALOG.write_text(json.dumps({"refreshed": datetime.datetime.now().isoformat(timespec="seconds"),
                                   "endpoints": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{len(entries)} endpoints in {CATALOG}")
    return entries


def load(log: Callable[[str], None] = print) -> dict:
    """The catalogue, built on first use."""
    if not CATALOG.exists():
        refresh(log)
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def search(entries: list[dict], words: list[str], kind: str | None = None) -> list[dict]:
    """Entries whose path holds every word (case-blind), optionally of one kind."""
    words = [w.lower() for w in words]
    return [e for e in entries if all(w in e["path"].lower() for w in words) and (kind is None or e["kind"] == kind)]
