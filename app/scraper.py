import hashlib, json, logging
import re
from datetime import datetime, timezone
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("jobpilot.scraper")
VALID_SOURCES = ["linkedin", "indeed", "glassdoor", "zip_recruiter",
                 "google", "bayt", "naukri", "bdjobs", "hellowork"]

def resolve_ca_cert():
    for name in ("JOBSPY_CA_CERT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = os.getenv(name)
        if value:
            path = Path(value).expanduser()
            if path.is_file():
                return str(path.resolve())
            log.warning("Ignoring %s because it is not a readable file: %s", name, path)
    if sys.platform == "win32":
        try:
            import certifi
            bundle = Path(tempfile.gettempdir()) / "jobpilot-windows-roots.pem"
            script = r"""
$certs = @(
    Get-ChildItem Cert:\CurrentUser\Root -ErrorAction SilentlyContinue
    Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue
) | Sort-Object Thumbprint -Unique
$lines = @()
foreach ($cert in $certs) {
    $lines += '-----BEGIN CERTIFICATE-----'
    $lines += [Convert]::ToBase64String($cert.RawData, [Base64FormattingOptions]::InsertLineBreaks) -split "`r`n"
    $lines += '-----END CERTIFICATE-----'
}
[IO.File]::WriteAllText($env:JOBPILOT_CA_OUTPUT, $lines -join "`n")
"""
            env = os.environ.copy()
            env["JOBPILOT_CA_OUTPUT"] = str(bundle)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
            )
            if result.returncode == 0 and bundle.is_file() and bundle.stat().st_size > 0:
                base = Path(certifi.where()).read_text(encoding="ascii", errors="ignore")
                windows_roots = bundle.read_text(encoding="utf-8", errors="ignore")
                bundle.write_text(base.rstrip() + "\n" + windows_roots + "\n", encoding="utf-8")
                return str(bundle.resolve())
            log.warning("Could not export Windows root certificates: %s", result.stderr.strip())
        except Exception as exc:
            log.warning("Could not create Windows CA bundle: %s", exc)
    return None

COUNTRY_ALIASES = {
    "india": "india", "in": "india", "bharat": "india",
    "usa": "usa", "us": "usa", "united states": "usa", "united states of america": "usa",
    "uk": "uk", "united kingdom": "uk", "great britain": "uk",
    "uae": "united arab emirates", "united arab emirates": "united arab emirates",
}

CITY_ALIASES = {
    "bangalore": "bengaluru",
    "banglore": "bengaluru",
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
}

def normalize_location(location):
    text = " ".join(str(location or "").strip().split())
    return text or "India"

def normalized_location_terms(location):
    text = normalize_location(location)
    terms = []
    for part in text.split(","):
        term = part.strip().lower()
        if term:
            term = CITY_ALIASES.get(term, term)
            terms.append(term)
    return terms

def location_matches(job_location, requested_location):
    job_text = str(job_location or "").strip().lower()
    if not job_text:
        return False
    parts = normalized_location_terms(requested_location)
    if not parts:
        return True

    first = parts[0]
    if first in COUNTRY_ALIASES.values():
        if first == "uk":
            return "uk" in job_text or "united kingdom" in job_text or "great britain" in job_text
        if first == "usa":
            return "usa" in job_text or "united states" in job_text or first in job_text
        return first in job_text

    first_aliases = {first}
    for alias, canonical in CITY_ALIASES.items():
        if canonical == first:
            first_aliases.add(alias)
    if any(alias in job_text for alias in first_aliases):
        return True
    return "remote" in job_text

def infer_country(location):
    parts = [part.strip().lower() for part in normalize_location(location).split(",") if part.strip()]
    for part in reversed(parts):
        country = COUNTRY_ALIASES.get(part)
        if country:
            return country
    if len(parts) == 1:
        return COUNTRY_ALIASES.get(parts[0], "india")
    return "india"

def is_within_days(value, days=30):
    if value is None:
        return False
    text = str(value).strip()
    try:
        posted = date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return False
    return posted >= date.today() - timedelta(days=days)

EXPERIENCE_PRESETS = {
    "any": (None, None),
    "0-1": (0, 1),
    "1-3": (1, 3),
    "3-5": (3, 5),
    "5-8": (5, 8),
    "8+": (8, None),
}

def parse_experience_range(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in ("fresher", "entry level", "graduate")):
        return 0, 0
    integers = [int(number) for number in re.findall(r"\b\d+\b", text)]
    if len(integers) >= 2:
        return min(integers[:2]), max(integers[:2])
    if integers:
        return integers[0], integers[0]
    return None

def experience_matches(value, preset):
    if preset in ("", "any", None):
        return True
    requested = EXPERIENCE_PRESETS.get(str(preset).lower())
    if requested is None:
        return True
    parsed = parse_experience_range(value)
    if parsed is None:
        return True
    minimum, maximum = requested
    job_minimum, job_maximum = parsed
    if minimum is not None and job_maximum < minimum:
        return False
    if maximum is not None and job_minimum > maximum:
        return False
    return True

def _value(row, key, default=None):
    try:
        value = row.get(key, default)
        if value is None: return default
        # pandas NaN handling
        if isinstance(value, float) and value != value: return default
        return value
    except Exception:
        return default

def normalize_job(row):
    title = str(_value(row, "title", "Untitled")).strip()
    company = str(_value(row, "company", "") or "").strip()
    url = str(_value(row, "job_url", "") or "").strip()
    source = str(_value(row, "site", "") or "").strip()
    fingerprint = hashlib.sha256((url or "|".join([title, company, str(_value(row,"location",""))])).encode()).hexdigest()
    description = str(_value(row, "description", "") or "").strip()
    experience_range = str(_value(row, "experience_range", "") or "").strip()
    if not experience_range and description:
        match = re.search(
            r"(?:fresher|entry\s*level|graduate|(?:\d+\s*(?:-|to)\s*\d+|\d+\+)\s*(?:years?|yrs?)(?:\s*(?:of\s*)?experience)?)",
            description,
            flags=re.IGNORECASE,
        )
        if match:
            experience_range = match.group(0)
    skills = _value(row, "skills", [])
    if isinstance(skills, list): skills = ", ".join(map(str, skills))
    return {
        "fingerprint": fingerprint, "source": source, "title": title,
        "company": company, "location": str(_value(row,"location","") or ""),
        "date_posted": str(_value(row,"date_posted","") or ""),
        "job_url": url, "description": str(_value(row,"description","") or ""),
        "min_amount": _value(row,"min_amount"), "max_amount": _value(row,"max_amount"),
        "currency": _value(row,"currency"), "is_remote": int(bool(_value(row,"is_remote",False))),
        "experience_range": experience_range,
        "skills": str(skills or "")
    }

def run_scrape(search_term, location, sources, results_wanted=25, hours_old=720, experience="any",
               progress_callback=None):
    try:
        from jobspy import scrape_jobs
    except Exception as exc:
        raise RuntimeError(
            "JobSpy dependencies are not installed. Run: pip install -r requirements.txt. "
            f"Import error: {exc}"
        ) from exc
    sources = [s for s in sources if s in VALID_SOURCES]
    if not sources:
        raise ValueError("Select at least one supported job source.")
    location = normalize_location(location)
    ca_cert = resolve_ca_cert()

    def scrape_source(source):
        if progress_callback:
            progress_callback(source, "connecting", None, None)
        try:
            frame = scrape_jobs(
                site_name=[source],
                search_term=search_term,
                location=location,
                distance=50,
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_indeed=infer_country(location),
                fetch_description=str(experience).lower() != "any",
                verbose=1,
                ca_cert=ca_cert,
            )
            source_jobs = []
            for row in frame.to_dict(orient="records"):
                job = normalize_job(row)
                if (
                    is_within_days(job["date_posted"], 30)
                    and location_matches(job["location"], location)
                    and experience_matches(job["experience_range"], experience)
                ):
                    source_jobs.append(job)
            frame_errors = frame.attrs.get("errors", {}) if hasattr(frame, "attrs") else {}
            error = frame_errors.get(source) if isinstance(frame_errors, dict) else None
            if progress_callback:
                progress_callback(
                    source,
                    "failed" if error else "completed",
                    error,
                    len(source_jobs),
                )
            return source_jobs, frame.attrs, error
        except Exception as exc:
            if progress_callback:
                progress_callback(source, "failed", str(exc), 0)
            return [], {"errors": {source: str(exc)}, "warnings": {}}, str(exc)

    jobs = []
    metadata = {"errors": {}, "warnings": {}}
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as executor:
        futures = {executor.submit(scrape_source, source): source for source in sources}
        for future in as_completed(futures):
            source_jobs, source_metadata, error = future.result()
            jobs.extend(source_jobs)
            if error:
                metadata["errors"][futures[future]] = error
            metadata["warnings"].update(source_metadata.get("warnings", {}))
    return jobs, metadata
