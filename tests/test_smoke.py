import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db
from app.scraper import (
    resolve_ca_cert,
    normalize_location,
    infer_country,
    is_within_days,
    location_matches,
    experience_matches,
    parse_experience_range,
    _public_geo_matches,
    _public_source_rows,
    is_worldwide_location,
)

def test_health():
    init_db()
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert "sources" in response.json()
    assert {"jobicy", "remoteok", "arbeitnow_eu", "arbeitnow_uk"}.issubset(
        response.json()["sources"]
    )
    assert "bdjobs" not in response.json()["sources"]
    assert "hellowork" not in response.json()["sources"]

def test_home():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "JobPilot" in response.text

def test_search_validation():
    client = TestClient(app)
    response = client.post("/api/search", json={
        "search_term": "x", "location": "India",
        "sources": ["not-a-board"], "results_wanted": 10, "hours_old": 24
    })
    assert response.status_code in (400, 422)

def test_search_rejects_concurrent_requests():
    from app import main

    client = TestClient(app)
    assert main.SEARCH_LOCK.acquire(blocking=False)
    try:
        response = client.post("/api/search", json={
            "search_term": "python",
            "location": "India",
            "sources": ["jobicy"],
        })
    finally:
        main.SEARCH_LOCK.release()

    assert response.status_code == 409

def test_resolve_ca_cert(tmp_path, monkeypatch):
    cert = tmp_path / "corporate-ca.pem"
    cert.write_text("test certificate bundle", encoding="utf-8")
    monkeypatch.setenv("JOBSPY_CA_CERT", str(cert))

    assert resolve_ca_cert() == str(cert.resolve())

def test_location_defaults_and_country_inference():
    assert normalize_location("") == "Worldwide"
    assert normalize_location("  Pune,   India ") == "Pune, India"
    assert infer_country("Pune, India") == "india"
    assert infer_country("Austin, Texas, USA") == "usa"
    assert infer_country("Worldwide") == "usa"
    assert is_worldwide_location("Remote worldwide")

def test_search_defaults_to_worldwide_remote_and_recent_jobs(monkeypatch):
    from app import scraper
    from app.main import SearchRequest

    calls = {}

    class EmptyFrame:
        attrs = {}

        def to_dict(self, orient):
            assert orient == "records"
            return []

    def fake_scrape_jobs(**kwargs):
        calls.update(kwargs)
        return EmptyFrame()

    monkeypatch.setattr(scraper, "resolve_ca_cert", lambda: None)
    monkeypatch.setitem(
        sys.modules, "jobspy", SimpleNamespace(scrape_jobs=fake_scrape_jobs)
    )

    request = SearchRequest(search_term="Python")
    scraper.run_scrape(request.search_term, request.location, ["linkedin"])

    assert request.location == "Worldwide"
    assert request.hours_old == 720
    assert calls["location"] is None
    assert calls["is_remote"] is True
    assert calls["hours_old"] == 720

def test_recent_date_filter_uses_current_30_day_window():
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=29)).isoformat()
    old = (date.today() - timedelta(days=31)).isoformat()
    assert is_within_days(recent, 30)
    assert not is_within_days(old, 30)

def test_location_filter_rejects_fallback_and_accepts_matching_location():
    assert location_matches("Pune, India", "Pune, India")
    assert location_matches("Remote - India", "India")
    assert not location_matches("Bengaluru, India", "Pune, India")
    assert location_matches("Hyderabad, India", "India")

def test_experience_filter():
    assert parse_experience_range("3-5 Yrs") == (3, 5)
    assert parse_experience_range("Fresher") == (0, 0)
    assert parse_experience_range("10+") == (10, 10)
    assert experience_matches("3-5 years", "1-3")
    assert experience_matches("3-5 years", "3-5")
    assert not experience_matches("3-5 years", "8+")
    assert experience_matches("", "1-3")

def test_jobs_source_filter_accepts_comma_separated_sources():
    init_db()
    client = TestClient(app)
    response = client.get("/api/jobs?source=linkedin,naukri&limit=10")
    assert response.status_code == 200
    assert "items" in response.json()

def test_location_autocomplete_requires_search_text():
    init_db()
    client = TestClient(app)
    response = client.get("/api/locations?q=a")
    assert response.status_code == 200
    assert response.json() == []

def test_jobicy_public_feed_maps_global_remote_jobs(monkeypatch):
    from datetime import date
    from app import scraper

    payload = {
        "jobs": [{
            "jobTitle": "Python Engineer",
            "companyName": "Example",
            "jobGeo": "India, APAC",
            "url": "https://jobicy.com/jobs/123-example",
            "pubDate": date.today().isoformat(),
            "jobDescription": "Python engineer role",
            "jobIndustry": ["Engineering"],
        }]
    }
    monkeypatch.setattr(scraper, "_fetch_public_json", lambda url, ca_cert=None: payload)

    jobs = _public_source_rows("jobicy", "Python Engineer", "India", 10)

    assert len(jobs) == 1
    assert jobs[0]["source"] == "jobicy"
    assert jobs[0]["job_url"] == "https://jobicy.com/jobs/123-example"
    assert jobs[0]["search_location"] == "India"

def test_public_remote_geo_is_filtered_by_eligibility():
    assert _public_geo_matches("India, APAC", "Pune, India", "jobicy")
    assert not _public_geo_matches("Argentina, Brazil, Mexico", "India", "jobicy")
    assert _public_geo_matches("Remote worldwide", "London, UK", "remoteok")
    assert not _public_geo_matches("Remote - United States", "India", "remoteok")
    assert _public_geo_matches("Argentina, Brazil, Mexico", "Worldwide", "jobicy")

def test_jobs_can_be_refreshed_by_search_location(tmp_path, monkeypatch):
    from datetime import date
    from app import database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "jobs.db")
    database.init_db()
    with database.connect() as db:
        db.execute(
            """INSERT INTO jobs
               (fingerprint,source,title,location,date_posted,job_url,search_location)
               VALUES(?,?,?,?,?,?,?)""",
            ("global-remote", "jobicy", "Remote Engineer", "Canada / North America",
             date.today().isoformat(), "https://jobicy.com/jobs/123", "Worldwide"),
        )

    response = TestClient(app).get("/api/jobs?location=Worldwide&source=jobicy&days=30")

    assert response.status_code == 200
    assert response.json()["total"] == 1
