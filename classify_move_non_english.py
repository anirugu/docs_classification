#!/usr/bin/env python
import argparse
import concurrent.futures
import contextlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

# Path to Tesseract executable (used for OCR fallback on image-only PDFs).
# Adjust if Tesseract is installed to a different location.
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Maps Tesseract OSD script names to language names used by this classifier.
OSD_SCRIPT_TO_LANGUAGE = {
    "Han": "Chinese",
    "Hangul": "Korean",
    "Hiragana": "Japanese",
    "Katakana": "Japanese",
    "HanS": "Chinese",
    "HanT": "Chinese",
    "Cyrillic": "Russian",   # refined later by detect_language_from_text if text is available
    "Arabic": "Arabic/Persian/Urdu script",
    "Devanagari": "Hindi/Devanagari",
    "Latin": "",             # Latin script — need text to distinguish language
    "Common": "",
}


STOP_WORDS = {
    "English": set(
        "the and of to in is you that it he was for on are with as his they be at one have "
        "this from or had by not but what all were when we there can an your which their said "
        "if do will each about how up out them then she many some so these would other into has more".split()
    ),
    "French": set(
        "le de un etre et a il avoir ne je son que se qui ce dans en du elle au pour pas "
        "vous par sur faire plus dire me on mon lui nous comme mais pouvoir avec tout y aller voir en bien".split()
    ),
    "Spanish": set(
        "de la que el en y a los se del las un por con no una su para es al lo como mas o "
        "pero sus le ha me si sin sobre este ya entre cuando todo esta ser son dos tambien fue habia era muy".split()
    ),
    "German": set(
        "der die und in den von zu das mit sich des auf fur ist im dem nicht ein eine als auch "
        "es an werden aus er hat dass sie nach wird bei einer um am sind noch wie einem uber einen".split()
    ),
    "Italian": set(
        "di che e il la a un in per una non sono si le del da con al come lo gli ha ma se ho "
        "piu o era alla mi anche nel suo delle".split()
    ),
    "Portuguese": set(
        "de que o e do da em um para com nao uma os no se na por mais as dos como mas foi ao "
        "ele das tem a seu sua ou ser quando muito ha nos ja esta eu tambem".split()
    ),
    "Dutch": set(
        "de het een en van in is dat op te met voor zijn die niet aan als er maar om door bij ook wordt uit was ik je".split()
    ),
}


LANGUAGE_CODES = {
    "en": "English",
    "eng": "English",
    "en-us": "English",
    "en-gb": "English",
    "de": "German",
    "deu": "German",
    "ger": "German",
    "de-de": "German",
    "es": "Spanish",
    "spa": "Spanish",
    "es-es": "Spanish",
    "fr": "French",
    "fra": "French",
    "fre": "French",
    "it": "Italian",
    "ita": "Italian",
    "pt": "Portuguese",
    "por": "Portuguese",
    "pt-br": "Portuguese",
    "nl": "Dutch",
    "nld": "Dutch",
    "dut": "Dutch",
    "ru": "Russian",
    "ru-ru": "Russian",
    "rus": "Russian",
    "uk": "Ukrainian",
    "uk-ua": "Ukrainian",
    "ukr": "Ukrainian",
    "id": "Indonesian",
    "id-id": "Indonesian",
    "pl": "Polish",
    "pol": "Polish",
    "tr": "Turkish",
    "tur": "Turkish",
    "ja": "Japanese",
    "jpn": "Japanese",
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "zh-hans": "Chinese",
    "zh-hant": "Chinese",
    "zh-hk": "Chinese",
    "zh-mo": "Chinese",
    "zh-sg": "Chinese",
    "zh-tw": "Chinese",
    "zho": "Chinese",
    "chi": "Chinese",
    "cmn": "Chinese",
    "yue": "Chinese",
    "ko": "Korean",
    "kor": "Korean",
    "ar": "Arabic",
    "ara": "Arabic",
    "hi": "Hindi",
    "hin": "Hindi",
}


TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".xhtml", ".xml", ".rtf", ".csv", ".log"}
ZIP_XML_EXTENSIONS = {".docx", ".odt"}
PDF_EXTENSIONS = {".pdf"}
DJVU_EXTENSIONS = {".djvu", ".djv"}
CALIBRE_EBOOK_EXTENSIONS = {
    ".azw",
    ".azw3",
    ".azw4",
    ".cbz",
    ".cbr",
    ".chm",
    ".epub",
    ".fb2",
    ".fbz",
    ".lit",
    ".lrf",
    ".mobi",
    ".pdb",
    ".prc",
    ".pml",
    ".rb",
    ".snb",
    ".tcr",
}
DOC_EXTENSIONS = {".doc"}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS
    | ZIP_XML_EXTENSIONS
    | PDF_EXTENSIONS
    | DJVU_EXTENSIONS
    | CALIBRE_EBOOK_EXTENSIONS
    | DOC_EXTENSIONS
)
DEFAULT_PDF_PAGES = 15
DEFAULT_WORKERS = max(1, min(8, os.cpu_count() or 1))
CACHE_VERSION = 2


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_metadata_language(value):
    normalized_names = {language.lower(): language for language in set(LANGUAGE_CODES.values())}
    raw = clean_text(value).lower()
    for token in re.split(r"[,;/|]+", raw):
        code = token.strip().replace("_", "-")
        if code in ("", "x-default", "und", "unknown"):
            continue
        if code in LANGUAGE_CODES:
            return LANGUAGE_CODES[code]
        if code.startswith(("ru-", "rus-")):
            return "Russian"
        if code.startswith(("uk-", "ukr-")):
            return "Ukrainian"
        if "russian" in code:
            return "Russian"
        if "ukrainian" in code or "ukraine" in code:
            return "Ukrainian"
        if code.startswith(("zh-", "zho-", "chi-")):
            return "Chinese"
        if "chinese" in code or "mandarin" in code or "cantonese" in code:
            return "Chinese"
        if code in normalized_names:
            return normalized_names[code]
    return ""


def detect_language_from_text(text):
    text = clean_text(text)[:30000]
    han_count = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
    kana_count = sum("\u3040" <= ch <= "\u30ff" for ch in text)
    hangul_count = sum("\uac00" <= ch <= "\ud7af" for ch in text)
    cyrillic_count = sum("\u0400" <= ch <= "\u04ff" for ch in text)
    ukrainian_count = sum(ch.lower() in "\u0456\u0457\u0454\u0491" for ch in text)

    if kana_count >= 8:
        return "Japanese", 90
    if hangul_count >= 8:
        return "Korean", 90
    if han_count >= 12:
        non_space_count = sum(not ch.isspace() for ch in text)
        if han_count / max(1, non_space_count) > 0.05:
            return "Chinese", 90
    if cyrillic_count >= 20:
        if ukrainian_count >= 2:
            return "Ukrainian", 90
        return "Russian", 90

    if len(text) < 80:
        return "Unknown", 0

    total = len(text)
    lower = text.lower()

    if kana_count / total > 0.08:
        return "Japanese", 90
    if hangul_count / total > 0.08:
        return "Korean", 90
    if han_count / total > 0.08:
        return "Chinese", 90
    if cyrillic_count / total > 0.08:
        return ("Ukrainian" if ukrainian_count >= 2 else "Russian"), 90
    if sum("\u0900" <= ch <= "\u097f" for ch in text) / total > 0.08:
        return "Hindi/Devanagari", 90
    if sum("\u0600" <= ch <= "\u06ff" for ch in text) / total > 0.08:
        return "Arabic/Persian/Urdu script", 80

    words = re.findall(r"[a-z\u00c0-\u024f']+", lower)
    if len(words) < 40:
        return "Unknown", 0

    scores = {
        language: sum(word in stop_words for word in words)
        for language, stop_words in STOP_WORDS.items()
    }
    best_language, best_score = max(scores.items(), key=lambda item: item[1])
    second_score = sorted(scores.values(), reverse=True)[1]

    if best_score < 8 or best_score < second_score * 1.25:
        return "Unknown", int(best_score / max(1, len(words)) * 900)

    confidence = max(55, min(95, int(best_score / max(1, len(words)) * 900)))
    return best_language, confidence


def run_command(command, timeout=20):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="ignore",
        )
    except Exception:
        return ""

    return completed.stdout + completed.stderr


def read_epub(path):
    metadata_language = ""
    text_parts = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                if name.lower().endswith(".opf"):
                    data = archive.read(name)[:200000].decode("utf-8", "ignore")
                    match = re.search(
                        r"<dc:language[^>]*>(.*?)</dc:language>",
                        data,
                        re.IGNORECASE | re.DOTALL,
                    ) or re.search(
                        r"<language[^>]*>(.*?)</language>",
                        data,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if match:
                        metadata_language = clean_text(match.group(1))
                        break

            for name in names:
                if name.lower().endswith((".xhtml", ".html", ".htm")):
                    text_parts.append(archive.read(name)[:120000].decode("utf-8", "ignore"))
                    if sum(len(part) for part in text_parts) > 200000:
                        break
    except Exception:
        pass

    return metadata_language, clean_text(" ".join(text_parts))


def read_fb2(path):
    try:
        data = path.read_bytes()[:700000]
        text = data.decode("utf-8", "ignore")
        if len(text) < 100:
            text = data.decode("cp1251", "ignore")
        match = re.search(r"<lang>(.*?)</lang>", text, re.IGNORECASE | re.DOTALL)
        return clean_text(match.group(1)) if match else "", clean_text(text)
    except Exception:
        return "", ""


def read_calibre_metadata(path):
    if not shutil.which("ebook-meta"):
        return "", ""

    output = run_command(["ebook-meta", str(path)], timeout=15)
    match = re.search(r"Languages?\s*:\s*(.+)", output, re.IGNORECASE)
    return clean_text(match.group(1)) if match else "", output[:5000]


def read_mobi(path):
    return read_calibre_metadata(path)


def read_text_file(path):
    try:
        data = path.read_bytes()[:700000]
    except Exception:
        return "", ""

    for encoding in ("utf-8", "utf-16", "cp1252", "latin1"):
        try:
            return "", clean_text(data.decode(encoding, "ignore"))
        except Exception:
            continue
    return "", ""


def read_zip_xml(path):
    candidates = {
        ".docx": ("word/document.xml",),
        ".odt": ("content.xml",),
    }.get(path.suffix.lower(), ())
    text_parts = []

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for name in candidates:
                if name in names:
                    text_parts.append(archive.read(name)[:700000].decode("utf-8", "ignore"))
    except Exception:
        pass

    return "", clean_text(" ".join(text_parts))


def read_doc(path):
    """
    Hack to extract text from binary .doc files without external dependencies.
    Reads the raw binary data, decodes ignoring errors, and strips non-printable chars.
    """
    try:
        data = path.read_bytes()[:1000000] # Read up to 1MB
    except Exception:
        return "", ""
        
    text_latin1 = data.decode('latin-1', 'ignore')
    text_utf16 = data.decode('utf-16le', 'ignore')
    
    # Extract only readable alphabetical words to drop the binary garbage
    words = re.findall(r'[a-zA-Z\u00c0-\u024f]{4,}', text_latin1)
    words += re.findall(r'[a-zA-Z\u00c0-\u024f]{4,}', text_utf16)
    
    return "", clean_text(" ".join(words))


def read_with_calibre_convert(path):
    if not shutil.which("ebook-convert"):
        return "", ""

    try:
        with tempfile.TemporaryDirectory(prefix="language_classify_") as temp_dir:
            output_path = Path(temp_dir) / "output.txt"
            output = run_command(["ebook-convert", str(path), str(output_path)], timeout=90)
            if output_path.exists():
                return "", clean_text(output_path.read_text(encoding="utf-8", errors="ignore")[:700000])
            return "", output[:10000]
    except Exception:
        return "", ""


def read_pdf_metadata_language(path):
    try:
        data = path.read_bytes()[:2_000_000].decode("latin1", "ignore")
    except Exception:
        return ""

    patterns = [
        r"/Lang\s*\(([^)]+)\)",
        r"<dc:language>\s*<rdf:Bag>\s*<rdf:li[^>]*>([^<]+)</rdf:li>",
        r"<dc:language[^>]*>([^<]+)</dc:language>",
    ]

    for pattern in patterns:
        match = re.search(pattern, data, re.IGNORECASE | re.DOTALL)
        if match:
            return clean_text(match.group(1))

    return ""


def read_pdf_text(path, max_pages=DEFAULT_PDF_PAGES):
    metadata_language = read_pdf_metadata_language(path)

    if shutil.which("pdftotext"):
        try:
            with tempfile.TemporaryDirectory(prefix="language_classify_pdf_") as temp_dir:
                output_path = Path(temp_dir) / "output.txt"
                run_command(
                    ["pdftotext", "-f", "1", "-l", str(max_pages), str(path), str(output_path)],
                    timeout=30,
                )
                if output_path.exists():
                    return metadata_language, clean_text(
                        output_path.read_text(encoding="utf-8", errors="ignore")[:300000]
                    )
        except Exception:
            pass

    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name)
            text_parts = []
            with contextlib.redirect_stderr(io.StringIO()):
                reader = module.PdfReader(str(path))
                for page in reader.pages[:max_pages]:
                    text_parts.append(page.extract_text() or "")
            return metadata_language, clean_text(" ".join(text_parts))
        except Exception:
            continue

    return metadata_language, ""


OCR_ENABLED = os.path.isfile(TESSERACT_CMD)


def read_pdf_text_ocr(path, max_pages=2):
    """OCR fallback for image-only PDFs.

    Strategy:
      1. Render up to *max_pages* pages via PyMuPDF at 200 DPI.
      2. Run Tesseract in OSD mode to detect the writing script.
         OSD does not require language packs beyond 'osd'.
      3. If the script maps directly to a non-Latin language, return it.
      4. If the script is Latin (i.e. could be English/French/German/etc.),
         also run full OCR with 'eng' to extract actual words, then use the
         existing stop-word classifier to pin down the language.
    Returns (language, confidence, source) or ("", 0, "") on failure.
    """
    if not OCR_ENABLED:
        return "", 0, ""

    try:
        import fitz  # pymupdf
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    except ImportError:
        return "", 0, ""

    try:
        doc = fitz.open(str(path))
    except Exception:
        return "", 0, ""

    collected_text = []
    detected_script = ""

    try:
        pages_to_scan = list(doc)[:max_pages]
        for page in pages_to_scan:
            # Render at 200 DPI (matrix scale = 200/72 ≈ 2.78)
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # --- Script detection via OSD ---
            if not detected_script:
                try:
                    osd = pytesseract.image_to_osd(img, config="--psm 0")
                    match = re.search(r"Script:\s*(\S+)", osd)
                    if match:
                        detected_script = match.group(1)
                except Exception:
                    pass

            # --- Text extraction (eng only; good for Latin-script languages) ---
            try:
                text = pytesseract.image_to_string(img, lang="eng", config="--psm 3")
                collected_text.append(text)
            except Exception:
                pass

            # Stop early if we have enough words to confidently detect language
            full_text_so_far = clean_text(" ".join(collected_text))
            if full_text_so_far:
                lang, conf = detect_language_from_text(full_text_so_far)
                if lang != "Unknown" and conf >= 55:
                    break
            elif sum(len(t) for t in collected_text) > 3000:
                break
    finally:
        doc.close()

    # --- Map detected script to a language (non-Latin scripts are unambiguous) ---
    if detected_script and detected_script != "Latin" and detected_script != "Common":
        lang = OSD_SCRIPT_TO_LANGUAGE.get(detected_script, "")
        if lang:
            return lang, 85, "ocr-osd"

    # --- Latin script: use stop-word classifier on OCR'd text ---
    full_text = clean_text(" ".join(collected_text))
    if full_text:
        lang, confidence = detect_language_from_text(full_text)
        source = "ocr-text" if lang != "Unknown" else "ocr-insufficient"
        return lang, confidence, source

    return "", 0, ""


def read_djvu(path):
    if shutil.which("djvutxt"):
        output = run_command(["djvutxt", str(path)], timeout=45)
        return "", clean_text(output[:300000])

    return read_with_calibre_convert(path)


def classify(path, pdf_pages=DEFAULT_PDF_PAGES, skip_pdf_text=False):
    extension = path.suffix.lower()
    metadata_language = ""
    text = ""

    if extension == ".epub":
        metadata_language, text = read_epub(path)
    elif extension == ".fb2":
        metadata_language, text = read_fb2(path)
    elif extension == ".mobi":
        metadata_language, text = read_mobi(path)
    elif extension in PDF_EXTENSIONS:
        metadata_language = read_pdf_metadata_language(path)
        language = normalize_metadata_language(metadata_language)
        if language:
            return language, 95, "metadata"
        if not skip_pdf_text and pdf_pages > 0:
            _, text = read_pdf_text(path, max_pages=pdf_pages)
    elif extension in DJVU_EXTENSIONS:
        metadata_language, text = read_djvu(path)
    elif extension in CALIBRE_EBOOK_EXTENSIONS:
        metadata_language, text = read_calibre_metadata(path)
        if not metadata_language:
            _, text = read_with_calibre_convert(path)
    elif extension in TEXT_EXTENSIONS:
        metadata_language, text = read_text_file(path)
    elif extension in ZIP_XML_EXTENSIONS:
        metadata_language, text = read_zip_xml(path)
    elif extension in DOC_EXTENSIONS:
        metadata_language, text = read_doc(path)
    else:
        return "Unknown", 0, "unsupported"

    language = normalize_metadata_language(metadata_language)
    if language:
        return language, 95, "metadata"

    if extension in PDF_EXTENSIONS and not text:
        # OCR fallback: only fires for image-only PDFs where text extraction found nothing.
        if OCR_ENABLED and not skip_pdf_text:
            ocr_lang, ocr_conf, ocr_source = read_pdf_text_ocr(path, max_pages=pdf_pages)
            if ocr_lang and ocr_conf >= 55:
                return ocr_lang, ocr_conf, ocr_source
        return "Unknown", 0, "pdf-no-text"
    if extension in DJVU_EXTENSIONS and not text:
        return "Unknown", 0, "djvu-no-text-extractor"
    if extension in CALIBRE_EBOOK_EXTENSIONS and not text:
        return "Unknown", 0, "calibre-no-text"

    language, confidence = detect_language_from_text(text)
    return language, confidence, "text-sample" if language != "Unknown" else "insufficient-text"


def unique_destination(destination_directory, file_name):
    candidate = destination_directory / file_name
    if not candidate.exists():
        return candidate

    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    index = 1
    while True:
        candidate = destination_directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def fix_file_extension(path):
    """
    Reads file magic bytes to detect the true format. If the file is missing an extension
    or has the wrong extension, it renames the file to have the correct extension.
    """
    try:
        with path.open("rb") as f:
            header = f.read(8192)
    except Exception:
        return path

    detected_ext = None
    if header.startswith(b"%PDF-"):
        detected_ext = ".pdf"
    elif header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        detected_ext = ".doc"
    elif header.startswith(b"{\\rtf1"):
        detected_ext = ".rtf"
    elif header.startswith(b"PK\x03\x04"):
        if b"mimetypeapplication/epub+zip" in header:
            detected_ext = ".epub"
        elif b"word/" in header:
            detected_ext = ".docx"
        elif b"mimetypeapplication/vnd.oasis.opendocument.text" in header:
            detected_ext = ".odt"
    elif b"<FictionBook" in header:
        detected_ext = ".fb2"

    if detected_ext and path.suffix.lower() != detected_ext:
        # Use stem if we want to replace the wrong extension, or just append it if missing
        new_name = path.stem + detected_ext
        new_path = path.with_name(new_name)
        
        counter = 1
        while new_path.exists() and new_path != path:
            new_path = path.with_name(f"{path.stem}_{counter}{detected_ext}")
            counter += 1
            
        try:
            path.rename(new_path)
            return new_path
        except Exception:
            return path
            
    return path


def classify_file(path, pdf_pages, skip_pdf_text):
    language, confidence, source = classify(
        path,
        pdf_pages=pdf_pages,
        skip_pdf_text=skip_pdf_text,
    )
    return path, language, confidence, source


def cache_key(path, pdf_pages, skip_pdf_text):
    try:
        stat = path.stat()
    except OSError:
        return ""

    return "|".join(
        (
            str(CACHE_VERSION),
            str(path.resolve()).lower(),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(pdf_pages),
            str(bool(skip_pdf_text)),
        )
    )


def load_cache(cache_path):
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache_path, cache):
    try:
        temp_path = cache_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(cache_path)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Classify ebook/document files and move confidently non-English files into one folder."
    )
    parser.add_argument("target", nargs="?", default=".", help="Directory to scan.")
    parser.add_argument(
        "--destination",
        default="non-english",
        help="Destination directory name or path. Defaults to non-english.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Classify only; do not move files.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print every confidently non-English file with language, confidence, and source.",
    )
    parser.add_argument(
        "--supported-extensions",
        action="store_true",
        help="Print supported extensions and exit.",
    )
    parser.add_argument(
        "--pdf-pages",
        type=int,
        default=DEFAULT_PDF_PAGES,
        help=f"Number of PDF pages to sample for text detection. Defaults to {DEFAULT_PDF_PAGES}.",
    )
    parser.add_argument(
        "--skip-pdf-text",
        action="store_true",
        help="Use PDF metadata only. Much faster, but misses PDFs without language metadata.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of files to classify in parallel. Defaults to {DEFAULT_WORKERS}.",
    )
    parser.add_argument(
        "--cache",
        default=None,
        help=(
            "Override the cache file path. By default the cache is stored inside the target "
            "folder as .language_classify_cache.json so each folder gets its own isolated cache."
        ),
    )
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write the classification cache.")
    args = parser.parse_args()

    if args.supported_extensions:
        for extension in sorted(SUPPORTED_EXTENSIONS):
            print(extension)
        return

    target = Path(args.target).resolve()
    destination = Path(args.destination)
    if not destination.is_absolute():
        destination = target / destination
    destination = destination.resolve()

    # Per-folder cache stored in the script's directory, named after the target folder.
    # E.g. scanning Downloads\7679000 → .language_classify_cache_7679000.json
    # Use --cache to override the path entirely.
    if args.cache is not None:
        cache_path = Path(args.cache).resolve()
    else:
        cache_path = Path(__file__).parent / f".language_classify_cache_{target.name}.json"
    if not args.no_cache:
        print(f"Cache: {cache_path}")

    if not args.dry_run:
        destination.mkdir(exist_ok=True)

    files = [path for path in sorted(target.rglob("*")) if path.is_file()]
    moved = []
    skipped = []

    cache = {} if args.no_cache else load_cache(cache_path)

    files_to_scan = []
    for path in files:
        if (
            path.resolve() != Path(__file__).resolve()
            and destination not in path.resolve().parents
        ):
            files_to_scan.append(path)

    # Fix extensions before classification
    if not args.dry_run:
        fixed_files = []
        for path in files_to_scan:
            if not path.exists():
                print(f"  [skip] {path.name}: file not found, skipping.")
                continue
            fixed_files.append(fix_file_extension(path))
        files_to_scan = fixed_files

    workers = max(1, args.workers)
    pdf_pages = max(0, args.pdf_pages)
    classified = []
    files_to_classify = []
    for path in files_to_scan:
        key = cache_key(path, pdf_pages, args.skip_pdf_text)
        cached = cache.get(key) if key else None
        if cached and path.exists():
            # Guard: only trust cache if the file still exists on disk.
            # The key already encodes size+mtime, so a changed file won't match,
            # but an outright missing file should be reclassified, not skipped.
            language, confidence, source = cached
            classified.append((path, language, confidence, source))
        else:
            files_to_classify.append(path)

    if workers == 1:
        # Run sequentially in main process to avoid BrokenProcessPool on segfaults
        for path in files_to_classify:
            if not path.exists():
                print(f"  [skip] {path.name}: file not found, skipping.")
                continue
            try:
                classified.append(classify_file(path, pdf_pages, args.skip_pdf_text))
            except FileNotFoundError:
                print(f"  [skip] {path.name}: file not found during classification, skipping.")
            except Exception as e:
                print(f"Error processing {path.name}: {e}")
                classified.append((path, "Unknown", 0, "error"))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            try:
                classified = list(
                    executor.map(
                        classify_file,
                        files_to_classify,
                        [pdf_pages] * len(files_to_classify),
                        [args.skip_pdf_text] * len(files_to_classify),
                    )
                ) + classified
            except Exception as e:
                print(f"Process pool crashed: {e}")
                # Fallback to sequential if pool crashes
                for path in files_to_classify:
                    try:
                        classified.append(classify_file(path, pdf_pages, args.skip_pdf_text))
                    except Exception as ex:
                        classified.append((path, "Unknown", 0, "error"))

    if not args.no_cache:
        # Add/update entries for files just classified
        for path, language, confidence, source in classified:
            key = cache_key(path, pdf_pages, args.skip_pdf_text)
            if key:
                cache[key] = [language, confidence, source]

        # Prune stale entries whose file path no longer exists on disk.
        # Cache key layout: CACHE_VERSION|resolved_path|size|mtime_ns|pdf_pages|skip_pdf_text
        live_paths = {str(p.resolve()).lower() for p in files_to_scan}
        stale_keys = [
            k for k in list(cache.keys())
            if len(k.split("|")) >= 3 and k.split("|")[1] not in live_paths
        ]
        for k in stale_keys:
            del cache[k]
        if stale_keys:
            print(f"  [cache] Pruned {len(stale_keys)} stale cache entries for files no longer on disk.")

        save_cache(cache_path, cache)

    classified.sort(key=lambda item: item[0].name)

    for path, language, confidence, source in classified:
        is_non_english = language not in ("English", "Unknown") and confidence >= 55
        # Only move to Unknown/ when the file IS a supported type but language could not be
        # determined (e.g. image-only PDF, too-short text). Files with source=="unsupported"
        # have an extension we don't recognise even after magic-byte detection — leave them.
        is_unknown = language == "Unknown" and source != "unsupported"

        if is_non_english or is_unknown:
            moved.append((path.name, language, confidence, source))
            if not args.dry_run:
                if not path.exists():
                    print(f"  [skip] {path.name}: file not found, skipping move.")
                    continue
                safe_language = language.replace("/", "-").replace("\\", "-")
                lang_destination = destination / safe_language
                lang_destination.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(path), str(unique_destination(lang_destination, path.name)))
                except FileNotFoundError:
                    print(f"  [skip] {path.name}: file not found during move, skipping.")
                except Exception as e:
                    print(f"  [error] {path.name}: could not move — {e}")
        else:
            skipped.append((path.name, language, confidence, source))

    moved_by_language = Counter(language for _, language, _, _ in moved)
    skipped_by_language = Counter(language for _, language, _, _ in skipped)

    print(f"Scanned: {len(files)}")
    print(f"Moved: {len(moved)}")
    for language, count in moved_by_language.most_common():
        print(f"  {language}: {count}")
    if args.list:
        for file_name, language, confidence, source in moved:
            print(f"{file_name}\t{language}\t{confidence}\t{source}")
    print(f"Skipped: {len(skipped)}")
    for language, count in skipped_by_language.most_common():
        print(f"  {language}: {count}")

    if args.dry_run:
        print("Dry run only; no files moved.")
    else:
        print(f"Destination: {destination}")


if __name__ == "__main__":
    main()
