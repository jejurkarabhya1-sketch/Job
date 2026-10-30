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
)

def test_health():
    init_db()
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert "sources" in response.json()

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

def test_resolve_ca_cert(tmp_path, monkeypatch):
    cert = tmp_path / "corporate-ca.pem"
    cert.write_text("test certificate bundle", encoding="utf-8")
    monkeypatch.setenv("JOBSPY_CA_CERT", str(cert))

    assert resolve_ca_cert() == str(cert.resolve())

def test_location_defaults_and_country_inference():
    assert normalize_location("") == "India"
    assert normalize_location("  Pune,   India ") == "Pune, India"
    assert infer_country("Pune, India") == "india"
    assert infer_country("Austin, Texas, USA") == "usa"

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
