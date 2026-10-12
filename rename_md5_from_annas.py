#!/usr/bin/env python
import argparse
import contextlib
import ftplib
import html
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field as dc_field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

import redis
import requests


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


DEFAULT_TARGET = r"C:\Users\Anirudha\Downloads\books\to_scan\7581000"
DEFAULT_BASE_URL = "https://annas-archive.gl/md5"
DEFAULT_LIBGEN_JSON_URL = "https://libgen.li/json.php"
DEFAULT_BAD_GATEWAY_DELAY = 20.0
MD5_NAME = re.compile(r"^[a-fA-F0-9]{32}$")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
KNOWN_EXTENSIONS = {
    ".azw",
    ".azw3",
    ".azw4",
    ".cbz",
    ".cbr",
    ".chm",
    ".djv",
    ".djvu",
    ".doc",
    ".docx",
    ".epub",
    ".fb2",
    ".lit",
    ".mobi",
    ".pdf",
    ".rtf",
    ".txt",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Maps the normalised label text found in the Anna's Archive page body to an
# internal field name.  The parser uses this to extract structured metadata.
_BODY_LABEL_MAP: dict[str, str] = {
    "alternative filename": "alternative_filename",
    "alternative author": "author",
    "alternative publisher": "publisher",
    "alternative edition": "edition",
    "alternative description": "description",
    "date open sourced": "date_open_sourced",
}


class PageMetadataParser(HTMLParser):
    """Single-pass HTML parser that collects:
    - <head> meta tags (name/property + content)
    - <title> text
    - Structured body fields identified by 'Alternative X' / 'date open sourced'
      labels whose next non-empty sibling text node is the value.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False
        # Skip <script> / <style> content to avoid false positives in the
        # large JS bundle that Anna's Archive inlines into every page.
        self._in_skip_tag = False
        # General label → value state machine driven by _BODY_LABEL_MAP.
        self._pending_field: str | None = None
        # Collected body fields; 'description' is a list because it can repeat.
        self.body_meta: dict[str, object] = {}

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs = {key.lower(): value for key, value in attrs}
        tag_lower = tag.lower()

        if tag_lower == "title":
            self._in_title = True
            return

        if tag_lower in ("script", "style"):
            self._in_skip_tag = True
            return

        if tag_lower != "meta":
            return

        key = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if key and content:
            self.meta[key] = content

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower == "title":
            self._in_title = False
        elif tag_lower in ("script", "style"):
            self._in_skip_tag = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return

        if self._in_skip_tag:
            return

        stripped = data.strip()
        if not stripped:
            return

        # Check if this text node IS a known label.
        field = _BODY_LABEL_MAP.get(stripped.lower())
        if field is not None:
            self._pending_field = field
            return

        # If the previous text was a label, this text is its value.
        if self._pending_field:
            field = self._pending_field
            self._pending_field = None
            if field == "description":
                # Multiple description blocks can appear; collect all of them.
                existing: list[str] = self.body_meta.get("description", [])  # type: ignore[assignment]
                existing.append(stripped)
                self.body_meta["description"] = existing
            else:
                # First occurrence wins for every other field.
                self.body_meta.setdefault(field, stripped)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return clean_spaces(" ".join(self.title_parts))

    @property
    def alternative_filename(self) -> str:
        return self.body_meta.get("alternative_filename", "")  # type: ignore[return-value]


def clean_spaces(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def looks_like_md5_file(path):
    return path.is_file() and bool(MD5_NAME.fullmatch(path.stem))


def looks_like_md5_name(name):
    return bool(MD5_NAME.fullmatch(Path(name).stem))


def basename_from_description(description):
    description = clean_spaces(description)
    if not description:
        return ""

    value = description.strip().strip("'\"")
    if "\\" in value:
        name = PureWindowsPath(value).name
    elif "/" in value:
        name = PurePosixPath(value).name
    else:
        name = value

    name = clean_spaces(name)
    if not name or name.lower().startswith(("http://", "https://")):
        return ""
    return name


def basename_from_locator(locator):
    locator = clean_spaces(locator)
    if not locator:
        return ""
    return PureWindowsPath(locator).name if "\\" in locator else PurePosixPath(locator).name


def title_from_page_title(title):
    title = clean_spaces(title)
    title = re.sub(r"\s*[-|]\s*Anna.?s Archive\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*Anna.?s Archive\s*$", "", title, flags=re.IGNORECASE)
    title = clean_spaces(title)
    if "/" in title or "\\" in title:
        return basename_from_description(title)
    return title


def sanitize_filename(name, fallback_extension):
    name = clean_spaces(name)
    name = name.replace("\u2013", "-").replace("\u2014", "-")
    name = INVALID_FILENAME_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")

    if not name:
        return ""

    suffix = Path(name).suffix
    if suffix.lower() not in KNOWN_EXTENSIONS:
        suffix = ""

    if not suffix and fallback_extension:
        name = f"{name}{fallback_extension}"

    stem = Path(name).stem.strip(" .")
    suffix = Path(name).suffix
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"{stem}_"

    max_stem = max(1, 240 - len(suffix))
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")

    return f"{stem}{suffix}"


def choose_filename(page_html, fallback_extension):
    parser = PageMetadataParser()
    parser.feed(page_html)

    candidates = [
        # "Alternative filename" is a real path stored by Anna's Archive
        # (e.g. "zlib/no-category/Unknown/Title_12345.fb2").  We take only
        # the last path segment so the result is a plain filename + extension.
        ("alternative-filename", basename_from_description(parser.alternative_filename)),
        ("meta-description", basename_from_description(parser.meta.get("description", ""))),
        ("og-description", basename_from_description(parser.meta.get("og:description", ""))),
        ("og-title", title_from_page_title(parser.meta.get("og:title", ""))),
        ("title", title_from_page_title(parser.title)),
    ]

    for source, candidate in candidates:
        filename = sanitize_filename(candidate, fallback_extension)
        if filename:
            return filename, source

    return "", ""


# ---------------------------------------------------------------------------
# Book metadata extraction & writing
# ---------------------------------------------------------------------------

@dataclass
class BookMetadata:
    """Structured book metadata extracted from a single Anna's Archive page."""

    title: str = ""
    author: str = ""
    publisher: str = ""
    edition: str = ""
    descriptions: list[str] = dc_field(default_factory=list)
    date_open_sourced: str = ""

    @property
    def description(self) -> str:
        """All description blocks joined by a blank line."""
        return "\n\n".join(self.descriptions)

    def is_empty(self) -> bool:
        return not any([
            self.title, self.author, self.publisher,
            self.edition, self.descriptions, self.date_open_sourced,
        ])

    def as_dict(self) -> dict:
        d: dict = {}
        if self.title:
            d["title"] = self.title
        if self.author:
            d["author"] = self.author
        if self.publisher:
            d["publisher"] = self.publisher
        if self.edition:
            d["edition"] = self.edition
        if self.descriptions:
            d["description"] = self.description
        if self.date_open_sourced:
            d["date_open_sourced"] = self.date_open_sourced
        return d


def extract_page_metadata(page_html: str) -> BookMetadata:
    """Parse a full Anna's Archive MD5 page and return a BookMetadata."""
    parser = PageMetadataParser()
    parser.feed(page_html)

    # Best-effort title from page <title> or og:title
    title = title_from_page_title(parser.title)
    if not title:
        title = title_from_page_title(parser.meta.get("og:title", ""))

    return BookMetadata(
        title=title,
        author=str(parser.body_meta.get("author", "")),
        publisher=str(parser.body_meta.get("publisher", "")),
        edition=str(parser.body_meta.get("edition", "")),
        descriptions=list(parser.body_meta.get("description", [])),  # type: ignore[arg-type]
        date_open_sourced=str(parser.body_meta.get("date_open_sourced", "")),
    )


# EPUB namespace constants
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"


def _register_epub_namespaces() -> None:
    ET.register_namespace("", _OPF_NS)
    ET.register_namespace("dc", _DC_NS)
    ET.register_namespace("opf", _OPF_NS)


def write_epub_metadata(path: Path, metadata: BookMetadata) -> bool:
    """Update OPF metadata inside an EPUB file in-place.

    Uses only the standard library (zipfile + xml.etree.ElementTree).
    Returns True on success, False if the file could not be updated.
    """
    _register_epub_namespaces()
    try:
        with zipfile.ZipFile(path, "r") as zf:
            # Locate the OPF file via META-INF/container.xml
            try:
                container_xml = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
            except KeyError:
                return False

            m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', container_xml, re.IGNORECASE)
            if not m:
                return False
            opf_path = m.group(1)

            try:
                opf_bytes = zf.read(opf_path)
            except KeyError:
                return False

            all_info = zf.infolist()

        # --- Parse OPF ---
        opf_text = opf_bytes.decode("utf-8", errors="replace")
        # Strip the XML declaration so ET can parse it cleanly
        opf_no_decl = re.sub(r"^<\?xml[^?]*\?>\s*", "", opf_text, flags=re.IGNORECASE)
        root = ET.fromstring(opf_no_decl)

        meta_elem = root.find(f"{{{_OPF_NS}}}metadata")
        if meta_elem is None:
            # Try without namespace (some older EPUBs)
            meta_elem = root.find("metadata")
        if meta_elem is None:
            return False

        def _dc(tag: str) -> str:
            return f"{{{_DC_NS}}}{tag}"

        def _set_dc(tag: str, value: str) -> None:
            """Set a dc: element, creating it if absent."""
            if not value:
                return
            elem = meta_elem.find(_dc(tag))  # type: ignore[union-attr]
            if elem is None:
                elem = ET.SubElement(meta_elem, _dc(tag))  # type: ignore[arg-type]
            elem.text = value

        # Title
        if metadata.title:
            _set_dc("title", metadata.title)

        # Author(s) — replace all existing dc:creator entries
        if metadata.author:
            for old in list(meta_elem.findall(_dc("creator"))):
                meta_elem.remove(old)
            for author in re.split(r",\s*", metadata.author):
                author = author.strip()
                if author:
                    ET.SubElement(meta_elem, _dc("creator")).text = author

        if metadata.publisher:
            _set_dc("publisher", metadata.publisher)

        if metadata.description:
            _set_dc("description", metadata.description)

        if metadata.date_open_sourced:
            _set_dc("date", metadata.date_open_sourced)

        # --- Serialise back ---
        new_opf = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            root, encoding="unicode"
        )
        new_opf_bytes = new_opf.encode("utf-8")

        # --- Rewrite the ZIP atomically via a temp file ---
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            suffix=".epub", dir=path.parent, prefix="_tmp_meta_"
        )
        tmp_path = Path(tmp_path_str)
        try:
            os.close(tmp_fd)
            with zipfile.ZipFile(path, "r") as zf_in, \
                 zipfile.ZipFile(tmp_path, "w") as zf_out:
                # EPUB spec: 'mimetype' must be first and stored uncompressed.
                mimetype_info = next(
                    (i for i in all_info if i.filename == "mimetype"), None
                )
                if mimetype_info:
                    zf_out.writestr(
                        zipfile.ZipInfo("mimetype"),
                        zf_in.read("mimetype"),
                        compress_type=zipfile.ZIP_STORED,
                    )

                for info in all_info:
                    if info.filename == "mimetype":
                        continue  # already written above
                    data = new_opf_bytes if info.filename == opf_path else zf_in.read(info.filename)
                    zf_out.writestr(info, data)

            shutil.move(str(tmp_path), str(path))
            return True
        except Exception:
            tmp_path.unlink(missing_ok=True)
            return False

    except Exception:
        return False


def write_file_metadata(path: Path, metadata: BookMetadata) -> None:
    """Write metadata into the renamed file when the file format supports it."""
    if metadata.is_empty():
        return

    if path.suffix.lower() != ".epub":
        return

    ok = write_epub_metadata(path, metadata)
    if not ok:
        print(f"  warning: could not update EPUB OPF metadata for {path.name}")


def unique_destination(path):
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def unique_name(name, existing_names):
    if name not in existing_names:
        return name

    path = Path(name)
    stem = path.stem
    suffix = path.suffix
    index = 1
    while True:
        candidate = f"{stem}_{index}{suffix}"
        if candidate not in existing_names:
            return candidate
        index += 1


def redis_client_from_env():
    connection_string = os.environ.get("Yts__RedisConnectionString")
    if not connection_string:
        raise RuntimeError("Yts__RedisConnectionString environment variable is not set.")
    return redis.Redis.from_url(connection_string, decode_responses=True)


def cache_key(md5):
    return f"annas-archive:md5-resolved-filename:v2:{md5.lower()}"


def get_cached_filename(md5, client):
    key = cache_key(md5)
    cached = client.get(key)
    if not cached:
        return "", ""

    return cached, "redis"


def fetch_page(md5, base_url, timeout, bad_gateway_delay):
    url = f"{base_url.rstrip('/')}/{md5.lower()}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; md5-title-renamer/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = requests.get(url, timeout=timeout, headers=headers)
    if response.status_code == 502 and bad_gateway_delay > 0:
        print(f"  got 502 Bad Gateway; waiting {bad_gateway_delay:.1f}s before retry")
        time.sleep(bad_gateway_delay)
        response = requests.get(url, timeout=timeout, headers=headers)

    response.raise_for_status()
    if response.status_code != 200:
        raise RuntimeError(f"Unexpected status {response.status_code} from {url}")
    if not response.text.strip():
        raise RuntimeError(f"Empty response from {url}")

    return response.text, "network"


def fetch_libgen_catalog(file_id_start, count, json_url, timeout, batch_size, cache_path=None):
    """Return MD5 -> original filename mappings from Libgen's public JSON endpoint."""
    if cache_path and cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as stream:
            cached = json.load(stream)
        if isinstance(cached, dict) and cached:
            print(f"Loaded {len(cached)} Libgen mappings from {cache_path}")
            return cached

    mappings = {}
    ids = list(range(file_id_start, file_id_start + count))
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; md5-title-renamer/1.0)",
        "Accept": "application/json",
    }
    for offset in range(0, len(ids), batch_size):
        batch = ids[offset:offset + batch_size]
        response = requests.get(
            json_url,
            params={"object": "f", "ids": ",".join(map(str, batch))},
            timeout=timeout,
            headers=headers,
        )
        response.raise_for_status()
        records = response.json()
        if not isinstance(records, dict) or "error" in records:
            raise RuntimeError(f"Unexpected Libgen JSON response: {records}")

        for record in records.values():
            md5 = str(record.get("md5", "")).lower()
            filename = basename_from_locator(str(record.get("locator", "")))
            if MD5_NAME.fullmatch(md5) and filename:
                mappings[md5] = filename
        print(f"  fetched Libgen records {offset + 1}-{offset + len(batch)} of {count}")

    if cache_path:
        with cache_path.open("w", encoding="utf-8") as stream:
            json.dump(mappings, stream, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"Saved {len(mappings)} Libgen mappings to {cache_path}")
    return mappings


def cache_successful_filename(md5, client, filename):
    key = cache_key(md5)
    client.set(key, filename)


def iter_files(target, recursive):
    iterator = target.rglob("*") if recursive else target.iterdir()
    for path in sorted(iterator, key=lambda item: item.name.lower()):
        if path.is_file():
            yield path


def is_ftp_target(target):
    return target.lower().startswith("ftp://")


def connect_ftp(target, password_override=""):
    parsed = urlparse(target)
    if parsed.scheme.lower() != "ftp":
        raise ValueError(f"Unsupported FTP URL: {target}")
    if not parsed.hostname:
        raise ValueError(f"FTP URL is missing host: {target}")

    username = unquote(parsed.username or "anonymous")
    if parsed.password is not None:
        password = unquote(parsed.password)
    else:
        password = password_override or os.environ.get("ANNAS_FTP_PASSWORD", "")
    port = parsed.port or 21
    folder = unquote(parsed.path or "/")

    ftp = ftplib.FTP()
    ftp.connect(parsed.hostname, port, timeout=30)
    ftp.login(username, password)
    ftp.cwd(folder)
    return ftp, folder


def iter_ftp_file_names(ftp):
    """Returns a list of (name, size) tuples."""
    results = []
    try:
        for name, facts in ftp.mlsd():
            if facts.get("type") == "file":
                try:
                    size = int(facts.get("size", 0))
                except (ValueError, TypeError):
                    size = 0
                results.append((name, size))
        return sorted(results, key=lambda x: x[0].lower())
    except Exception:
        pass

    names = ftp.nlst()
    for name in names:
        base_name = PurePosixPath(name).name
        if not base_name or base_name in (".", ".."):
            continue
        try:
            size = ftp.size(base_name)
        except Exception:
            size = 0
        results.append((base_name, size or 0))
    # Remove duplicates from nlst results if any, then sort by name
    unique_results = {name: size for name, size in results}
    return sorted(unique_results.items(), key=lambda x: x[0].lower())


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def rename_md5_files(args):
    if is_ftp_target(args.target):
        rename_md5_ftp_files(args)
        return

    target = Path(args.target).resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    files = [path for path in iter_files(target, args.recursive) if looks_like_md5_file(path)]
    # Sort by size descending: process large files first.
    files.sort(key=lambda p: p.stat().st_size, reverse=True)

    print(f"Target: {target}")
    print(f"MD5-named files: {len(files)}")
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")

    libgen_mappings = {}
    client = None
    if args.libgen_file_id_start is not None:
        cache_path = target / args.libgen_cache_name
        libgen_mappings = fetch_libgen_catalog(
            args.libgen_file_id_start,
            args.libgen_count,
            args.libgen_json_url,
            args.timeout,
            args.libgen_batch_size,
            cache_path,
        )
    else:
        client = redis_client_from_env()
        client.ping()

    renamed = 0
    skipped = 0
    failed = 0

    for index, path in enumerate(files, start=1):
        md5 = path.stem.lower()
        page_source = ""
        try:
            if not path.is_file():
                skipped += 1
                print(f"[{index}/{len(files)}] skip {path.name}: file no longer exists")
                continue

            size_str = format_size(path.stat().st_size)
            if md5 in libgen_mappings:
                new_name = sanitize_filename(libgen_mappings[md5], path.suffix)
                name_source = "libgen-locator"
                page_source = "libgen-json"
                metadata = BookMetadata()
            else:
                cached_filename, cached_source = get_cached_filename(md5, client) if client else ("", "")
            if md5 not in libgen_mappings and cached_filename:
                new_name = sanitize_filename(cached_filename, path.suffix)
                name_source = cached_source
                page_source = "redis-filename"
                metadata = BookMetadata()  # metadata not available from cache
            elif md5 not in libgen_mappings:
                if args.libgen_file_id_start is not None:
                    skipped += 1
                    print(f"[{index}/{len(files)}] skip {path.name} ({size_str}): not in Libgen range")
                    continue
                page_html, page_source = fetch_page(
                    md5, args.base_url, args.timeout, args.bad_gateway_delay
                )
                new_name, name_source = choose_filename(page_html, path.suffix)
                metadata = extract_page_metadata(page_html)
                if new_name and client:
                    cache_successful_filename(md5, client, new_name)

            if not new_name:
                skipped += 1
                print(f"[{index}/{len(files)}] skip {path.name} ({size_str}): no usable title")
            else:
                destination = unique_destination(path.with_name(new_name))
                if destination.name == path.name:
                    skipped += 1
                    print(f"[{index}/{len(files)}] skip {path.name} ({size_str}): already matches")
                elif args.apply:
                    path.rename(destination)
                    write_file_metadata(destination, metadata)
                    print(
                        f"[{index}/{len(files)}] renamed {path.name} -> {destination.name} "
                        f"({size_str}, {name_source}, {page_source})"
                    )
                    renamed += 1
                else:
                    print(
                        f"[{index}/{len(files)}] would rename {path.name} -> {destination.name} "
                        f"({size_str}, {name_source}, {page_source})"
                    )
                    renamed += 1

            if page_source == "network" and index < len(files):
                delay = random.uniform(args.delay_min, args.delay_max)
                print(f"  waiting {delay:.1f}s before next network call")
                time.sleep(delay)
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(files)}] error {path.name}: {exc}")

    action = "Renamed" if args.apply else "Would rename"
    print(f"{action}: {renamed}")
    print(f"Skipped: {skipped}")
    print(f"Failed: {failed}")


def rename_md5_ftp_files(args):
    if args.recursive:
        raise ValueError("FTP targets currently support one folder at a time; remove --recursive.")

    client = redis_client_from_env()
    client.ping()

    ftp, folder = connect_ftp(args.target, args.ftp_password)
    try:
        all_info = iter_ftp_file_names(ftp)
        existing_names = {name for name, size in all_info}
        # Filter for MD5-named files and sort by size descending
        files_info = [info for info in all_info if looks_like_md5_name(info[0])]
        files_info.sort(key=lambda x: x[1], reverse=True)

        print(f"Target: {args.target}")
        print(f"FTP folder: {folder}")
        print(f"MD5-named files: {len(files_info)}")
        print(f"Mode: {'apply' if args.apply else 'dry-run'}")

        renamed = 0
        skipped = 0
        failed = 0

        for index, (name, size) in enumerate(files_info, start=1):
            md5 = Path(name).stem.lower()
            suffix = Path(name).suffix
            page_source = ""
            size_str = format_size(size)
            try:
                cached_filename, cached_source = get_cached_filename(md5, client)
                if cached_filename:
                    new_name = sanitize_filename(cached_filename, suffix)
                    name_source = cached_source
                    page_source = "redis-filename"
                else:
                    page_html, page_source = fetch_page(
                        md5, args.base_url, args.timeout, args.bad_gateway_delay
                    )
                    new_name, name_source = choose_filename(page_html, suffix)
                    if new_name:
                        cache_successful_filename(md5, client, new_name)

                if not new_name:
                    skipped += 1
                    print(f"[{index}/{len(files_info)}] skip {name} ({size_str}): no usable title")
                else:
                    destination_name = unique_name(new_name, existing_names)
                    if destination_name == name:
                        skipped += 1
                        print(f"[{index}/{len(files_info)}] skip {name} ({size_str}): already matches")
                    elif args.apply:
                        ftp.rename(name, destination_name)
                        existing_names.discard(name)
                        existing_names.add(destination_name)
                        print(
                            f"[{index}/{len(files_info)}] renamed {name} -> {destination_name} "
                            f"({size_str}, {name_source}, {page_source})"
                        )
                        renamed += 1
                    else:
                        print(
                            f"[{index}/{len(files_info)}] would rename {name} -> {destination_name} "
                            f"({size_str}, {name_source}, {page_source})"
                        )
                        renamed += 1

                if page_source == "network" and index < len(files_info):
                    delay = random.uniform(args.delay_min, args.delay_max)
                    print(f"  waiting {delay:.1f}s before next network call")
                    time.sleep(delay)
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(files)}] error {name}: {exc}")

        action = "Renamed" if args.apply else "Would rename"
        print(f"{action}: {renamed}")
        print(f"Skipped: {skipped}")
        print(f"Failed: {failed}")
    finally:
        with contextlib.suppress(Exception):
            ftp.quit()


def main():
    parser = argparse.ArgumentParser(
        description="Rename MD5-named local files using cached Anna's Archive metadata pages."
    )
    parser.add_argument("target", nargs="?", default=DEFAULT_TARGET, help="Folder to scan.")
    parser.add_argument("--apply", action="store_true", help="Actually rename files. Default is dry-run.")
    parser.add_argument("--recursive", action="store_true", help="Scan subfolders too.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Anna's Archive MD5 base URL.")
    parser.add_argument(
        "--libgen-file-id-start",
        type=int,
        default=None,
        help="First global Libgen file ID for a sequential torrent range.",
    )
    parser.add_argument("--libgen-count", type=int, default=1000, help="Number of Libgen IDs to fetch.")
    parser.add_argument("--libgen-batch-size", type=int, default=100, help="IDs per Libgen JSON request.")
    parser.add_argument("--libgen-json-url", default=DEFAULT_LIBGEN_JSON_URL, help="Libgen JSON endpoint.")
    parser.add_argument(
        "--libgen-cache-name",
        default=".libgen_filename_mappings.json",
        help="Filename for the local reusable Libgen mapping cache.",
    )
    parser.add_argument("--delay-min", type=float, default=5.0, help="Minimum delay after network calls.")
    parser.add_argument("--delay-max", type=float, default=10.0, help="Maximum delay after network calls.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--bad-gateway-delay",
        type=float,
        default=DEFAULT_BAD_GATEWAY_DELAY,
        help="Seconds to wait before retrying once after a 502 Bad Gateway response.",
    )
    parser.add_argument(
        "--ftp-password",
        default="",
        help="FTP password when the target is ftp:// and the URL does not include a password.",
    )
    args = parser.parse_args()

    if args.delay_min < 0 or args.delay_max < 0:
        raise ValueError("Delay values must be non-negative.")
    if args.delay_min > args.delay_max:
        raise ValueError("--delay-min cannot be greater than --delay-max.")
    if args.bad_gateway_delay < 0:
        raise ValueError("--bad-gateway-delay must be non-negative.")
    if args.libgen_count <= 0 or args.libgen_batch_size <= 0:
        raise ValueError("Libgen count and batch size must be positive.")

    rename_md5_files(args)


if __name__ == "__main__":
    main()
