import hashlib, json, logging, ssl, urllib.parse, urllib.request
import re
from datetime import datetime
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("jobpilot.scraper")
VALID_SOURCES = ["linkedin", "indeed", "glassdoor", "zip_recruiter",
                 "google", "bayt", "naukri",
                 "jobicy", "remoteok", "arbeitnow_eu", "arbeitnow_uk"]
JOBSPY_SOURCES = set(VALID_SOURCES) - {
    "jobicy", "remoteok", "arbeitnow_eu", "arbeitnow_uk"
}
PUBLIC_SOURCE_URLS = {
    "jobicy": "https://jobicy.com/api/jobs",
    "remoteok": "https://remoteok.com/api",
    "arbeitnow_eu": "https://arbeitnow.com/api/job-board-api",
    "arbeitnow_uk": "https://arbeitnow.co.uk/api/job-board-api",
}

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
    return text or "Worldwide"

def is_worldwide_location(location):
    return normalize_location(location).strip().lower() in {
        "worldwide", "global", "anywhere", "remote worldwide", "remote anywhere"
    }

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
    if is_worldwide_location(location):
        return "usa"
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
        "skills": str(skills or ""), "search_location": ""
    }

def _fetch_public_json(url, ca_cert=None):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "JobPilot/1.0 (public job search client)",
        },
    )
    context = ssl.create_default_context(cafile=ca_cert) if ca_cert else None
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        return json.loads(response.read().decode("utf-8"))

def _public_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]

def _public_geo_matches(job_location, requested_location, source):
    location = str(job_location or "").strip().lower()
    requested = normalize_location(requested_location).lower()
    if is_worldwide_location(requested_location):
        return True
    if not location or any(marker in location for marker in ("worldwide", "anywhere", "global", "everywhere")):
        return True
    if location in ("remote", "remote worldwide", "remote anywhere"):
        return True
    if source == "arbeitnow_eu" and requested in ("europe", "eu", "european union"):
        return True
    if source == "arbeitnow_uk" and requested in ("uk", "united kingdom", "great britain"):
        return True
    if source == "jobicy":
        requested_terms = normalized_location_terms(requested_location)
        requested_country = next(
            (COUNTRY_ALIASES[term] for term in reversed(requested_terms) if term in COUNTRY_ALIASES),
            "",
        )
        regions = {
            "india": ("asia", "apac", "asia-pacific"),
            "usa": ("north america", "americas"),
            "uk": ("europe", "emea"),
            "united kingdom": ("europe", "emea"),
            "united arab emirates": ("middle east", "emea"),
        }
        country_regions = {
            "canada": ("north america", "americas"),
            "australia": ("asia-pacific", "apac"),
            "new zealand": ("asia-pacific", "apac"),
            "germany": ("europe", "emea"),
            "france": ("europe", "emea"),
            "spain": ("europe", "emea"),
            "italy": ("europe", "emea"),
            "netherlands": ("europe", "emea"),
            "brazil": ("latin america", "latam"),
            "mexico": ("latin america", "latam"),
            "singapore": ("asia", "apac", "asia-pacific"),
            "japan": ("asia", "apac", "asia-pacific"),
        }
        targets = set(requested_terms)
        if requested_country:
            targets.add(requested_country)
            targets.update(regions.get(requested_country, ()))
            targets.update({
                "usa": ("united states", "united states of america"),
                "uk": ("united kingdom", "great britain"),
                "united arab emirates": ("uae",),
            }.get(requested_country, ()))
        for term in requested_terms:
            targets.update(country_regions.get(term, ()))
        if "europe" in requested_terms:
            targets.update(("uk", "united kingdom", "germany", "france", "spain", "italy", "netherlands"))
        return any(target and target in location for target in targets)
    if source == "remoteok":
        restricted_location = re.sub(
            r"\b(remote|remote-only|work from anywhere|work remotely)\b",
            "",
            location,
        ).strip(" -,:()")
        if not restricted_location:
            return True
        location = restricted_location
    if source == "remoteok":
        return location_matches(restricted_location, requested_location)
    return location_matches(job_location, requested_location)

def _public_source_rows(source, search_term, location, results_wanted, ca_cert=None):
    url = PUBLIC_SOURCE_URLS[source]
    if source == "jobicy":
        query = urllib.parse.urlencode({
            "count": min(100, max(results_wanted, 25)),
            "tag": search_term,
        })
        url = f"{url}?{query}"
    payload = _fetch_public_json(url, ca_cert)
    if source == "jobicy":
        items = payload.get("jobs", [])
    elif source == "remoteok":
        items = payload[1:] if isinstance(payload, list) else []
    else:
        items = payload.get("data", [])

    term_tokens = [token.lower() for token in search_term.split() if token]
    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if source == "jobicy":
            title = item.get("jobTitle", "")
            company = item.get("companyName", "")
            job_location = item.get("jobGeo", "")
            description = item.get("jobDescription") or item.get("jobExcerpt") or ""
            job_url = item.get("url", "")
            date_posted = item.get("pubDate", "")
            tags = item.get("jobIndustry", [])
            is_remote = True
        elif source == "remoteok":
            title = item.get("position", "")
            company = item.get("company", "")
            job_location = item.get("location") or "Remote worldwide"
            description = item.get("description", "")
            slug = item.get("slug", "")
            job_url = item.get("url") or (
                f"https://remoteok.com/remote-jobs/{slug}" if slug else ""
            )
            date_posted = item.get("date", "")
            tags = item.get("tags", [])
            is_remote = True
        else:
            title = item.get("title", "")
            company = item.get("company_name", "")
            job_location = item.get("location") or ("Remote" if item.get("remote") else "")
            description = item.get("description", "")
            slug = item.get("slug", "")
            domain = "arbeitnow.co.uk" if source == "arbeitnow_uk" else "arbeitnow.com"
            job_url = item.get("url") or (
                f"https://www.{domain}/jobs/{slug}" if slug else ""
            )
            date_posted = item.get("created_at") or item.get("date", "")
            tags = item.get("tags", [])
            is_remote = bool(item.get("remote"))

        text = " ".join(
            [str(title), str(company), " ".join(map(str, tags or [])), str(description)]
        ).lower()
        if not all(token in text for token in term_tokens):
            continue
        if not _public_geo_matches(job_location, location, source):
            continue
        row = {
            "title": title,
            "company": company,
            "location": job_location or "Remote",
            "site": source,
            "job_url": job_url,
            "date_posted": _public_date(date_posted),
            "description": description,
            "is_remote": is_remote,
            "skills": tags,
            "min_amount": item.get("salaryMin") or item.get("salary_min"),
            "max_amount": item.get("salaryMax") or item.get("salary_max"),
            "currency": item.get("salaryCurrency") or item.get("salary_currency"),
        }
        job = normalize_job(row)
        if is_within_days(job["date_posted"], 30):
            job["search_location"] = normalize_location(location)
            jobs.append(job)
        if len(jobs) >= results_wanted:
            break
    return jobs

def run_scrape(search_term, location, sources, results_wanted=25, hours_old=720, experience="any",
               progress_callback=None):
    sources = [s for s in sources if s in VALID_SOURCES]
    if not sources:
        raise ValueError("Select at least one supported job source.")
    location = normalize_location(location)
    worldwide = is_worldwide_location(location)
    ca_cert = resolve_ca_cert()

    def scrape_source(source):
        if progress_callback:
            progress_callback(source, "connecting", None, None)
        try:
            if source not in JOBSPY_SOURCES:
                source_jobs = _public_source_rows(
                    source, search_term, location, results_wanted, ca_cert
                )
                source_metadata = {"errors": {}, "warnings": {}}
                error = None
            else:
                try:
                    from jobspy import scrape_jobs
                except Exception as exc:
                    raise RuntimeError(
                        "JobSpy dependencies are not installed. Run: pip install -r requirements.txt. "
                        f"Import error: {exc}"
                    ) from exc
                frame = scrape_jobs(
                    site_name=[source],
                    search_term=search_term,
                    location=None if worldwide else location,
                    distance=50,
                    results_wanted=results_wanted,
                    hours_old=hours_old,
                    country_indeed=infer_country(location),
                    is_remote=False,
                    fetch_description=str(experience).lower() != "any",
                    verbose=1,
                    ca_cert=ca_cert,
                )
                source_jobs = []
                for row in frame.to_dict(orient="records"):
                    job = normalize_job(row)
                    if (
                        is_within_days(job["date_posted"], 30)
                        and (worldwide or location_matches(job["location"], location))
                        and experience_matches(job["experience_range"], experience)
                    ):
                        source_jobs.append(job)
                source_metadata = frame.attrs
                frame_errors = source_metadata.get("errors", {}) if hasattr(source_metadata, "get") else {}
                error = frame_errors.get(source) if isinstance(frame_errors, dict) else None
            source_jobs = [
                job for job in source_jobs
                if experience_matches(job["experience_range"], experience)
            ]
            for job in source_jobs:
                job["search_location"] = location
            if progress_callback:
                progress_callback(
                    source,
                    "failed" if error else "completed",
                    error,
                    len(source_jobs),
                )
            return source_jobs, source_metadata, error
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
