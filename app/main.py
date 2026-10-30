import json, logging, time, urllib.parse, urllib.request
from pathlib import Path
from threading import Lock
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app.database import init_db, connect
from app.scraper import run_scrape, VALID_SOURCES

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="JobPilot", version="0.1.0", description="Multi-board job search powered by your uploaded JobSpy source.", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
LOCATION_CACHE = {}
SEARCH_STATUS = {}
SEARCH_LOCK = Lock()

class SearchRequest(BaseModel):
    search_term: str = Field(min_length=2, max_length=120)
    location: str = Field(default="Worldwide", max_length=120)
    sources: list[str] = Field(default_factory=lambda: ["linkedin","indeed","jobicy","remoteok"])
    results_wanted: int = Field(default=25, ge=1, le=100)
    hours_old: int = Field(default=720, ge=1, le=720)
    experience: str = Field(default="any", pattern="^(any|0-1|1-3|3-5|5-8|8\\+)$")

def save_jobs(items):
    with connect() as db:
        for j in items:
            db.execute("""
            INSERT INTO jobs(fingerprint,source,title,company,location,date_posted,job_url,description,
                min_amount,max_amount,currency,is_remote,experience_range,search_location,skills,last_seen_at)
            VALUES(:fingerprint,:source,:title,:company,:location,:date_posted,:job_url,:description,
                :min_amount,:max_amount,:currency,:is_remote,:experience_range,:search_location,:skills,CURRENT_TIMESTAMP)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen_at=CURRENT_TIMESTAMP,
                description=CASE WHEN excluded.description!='' THEN excluded.description ELSE jobs.description END,
                experience_range=CASE WHEN excluded.experience_range!='' THEN excluded.experience_range ELSE jobs.experience_range END,
                search_location=CASE WHEN excluded.search_location!='' THEN excluded.search_location ELSE jobs.search_location END
            """, j)

def execute_search(req, release_search_lock=False):
    try:
        return _execute_search(req)
    finally:
        if release_search_lock:
            SEARCH_LOCK.release()

def _execute_search(req):
    started = time.time()
    search_id = str(int(time.time()*1000))
    SEARCH_STATUS.clear()
    SEARCH_STATUS.update({
        "id": search_id,
        "status": "running",
        "sources": {
            source: {"status": "connecting", "count": 0, "error": None}
            for source in req.sources
        },
    })
    def progress(source, status, error, count):
        portal = SEARCH_STATUS["sources"].get(source)
        if portal is None:
            return
        portal["status"] = status
        portal["error"] = error
        portal["count"] = count if count is not None else 0
    try:
        jobs, metadata = run_scrape(
            req.search_term,
            req.location,
            req.sources,
            req.results_wanted,
            req.hours_old,
            req.experience,
            progress,
        )
        with connect() as db:
            failed_sources = set(metadata.get("errors", {}))
            successful_sources = [
                source for source in req.sources if source not in failed_sources
            ]
            if successful_sources:
                placeholders = ",".join("?" for _ in successful_sources)
                db.execute(
                    f"DELETE FROM jobs WHERE lower(source) IN ({placeholders})",
                    [source.lower() for source in successful_sources],
                )
        save_jobs(jobs)
        status = "completed"
        error = json.dumps({"errors": metadata.get("errors",{}), "warnings": metadata.get("warnings",{})}, default=str)
        count = len(jobs)
    except Exception as exc:
        jobs, count, status, error = [], 0, "failed", str(exc)
        logging.exception("Search failed")
    with connect() as db:
        db.execute("INSERT INTO searches(search_term,location,sources,result_count,status,error) VALUES(?,?,?,?,?,?)",
                   (req.search_term, req.location, ",".join(req.sources), count, status, error))
    SEARCH_STATUS["status"] = status
    SEARCH_STATUS["elapsed_seconds"] = round(time.time()-started, 2)
    SEARCH_STATUS["count"] = count
    return {"status": status, "count": count, "elapsed_seconds": round(time.time()-started,2), "error": error if status=="failed" else None}

@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#526df5"/><text x="32" y="44" text-anchor="middle" font-family="Arial,sans-serif" font-size="38" font-weight="700" fill="white">J</text></svg>'
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/api/health")
def health():
    try:
        from jobspy import scrape_jobs
        jobspy_status="available"
    except Exception as exc:
        jobspy_status=f"unavailable: {exc}"
    return {"status":"ok","jobspy":jobspy_status,"sources":VALID_SOURCES}

@app.get("/api/stats")
def stats():
    with connect() as db:
        total=db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        recent=db.execute("SELECT COUNT(*) FROM jobs WHERE created_at >= datetime('now','-7 days')").fetchone()[0]
        sources=db.execute("SELECT COUNT(DISTINCT source) FROM jobs").fetchone()[0]
        latest=db.execute("SELECT created_at FROM searches ORDER BY id DESC LIMIT 1").fetchone()
        return {"total_jobs":total,"new_last_7_days":recent,"sources":sources,"last_search":latest[0] if latest else None}

@app.get("/api/locations")
def location_suggestions(q: str):
    query = " ".join(q.strip().split())
    if len(query) < 2:
        return []
    cache_key = query.lower()
    cached = LOCATION_CACHE.get(cache_key)
    if cached is not None and time.time() - cached["time"] < 3600:
        return cached["items"]
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 8,
        "accept-language": "en",
    })
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={
            "User-Agent": "JobPilot/1.0 (job search location autocomplete)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        items = []
        for entry in data:
            address = entry.get("address") or {}
            parts = [
                address.get("city") or address.get("town") or address.get("village")
                or address.get("county") or address.get("state_district"),
                address.get("state"),
                address.get("country"),
            ]
            label = ", ".join(part for part in parts if part)
            if not label:
                label = entry.get("display_name", "")
            if label:
                items.append({
                    "label": label,
                    "display_name": entry.get("display_name", ""),
                    "latitude": entry.get("lat"),
                    "longitude": entry.get("lon"),
                    "type": entry.get("type"),
                })
        unique = []
        seen = set()
        for item in items:
            if item["label"] not in seen:
                seen.add(item["label"])
                unique.append(item)
        LOCATION_CACHE[cache_key] = {"time": time.time(), "items": unique}
        return unique
    except Exception as exc:
        logging.warning("Location lookup failed for %r: %s", query, exc)
        return []

@app.post("/api/search")
def search(req: SearchRequest, background_tasks: BackgroundTasks):
    invalid=set(req.sources)-set(VALID_SOURCES)
    if invalid: raise HTTPException(400, f"Unsupported sources: {sorted(invalid)}")
    if not SEARCH_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A search is already running. Wait for it to finish before starting another.")
    # Background execution avoids holding the HTTP request open during multi-board scraping.
    try:
        background_tasks.add_task(execute_search, req, True)
    except Exception:
        SEARCH_LOCK.release()
        raise
    return {"status":"started","message":"Search queued. Refresh jobs to see results."}

@app.get("/api/search/status")
def search_status():
    return SEARCH_STATUS.copy()

@app.get("/api/jobs")
def jobs(q: Optional[str]=None, location: Optional[str]=None, source: Optional[str]=None,
         limit:int=100, offset:int=0, days:int=30):
    limit=max(1,min(limit,500)); offset=max(0,offset)
    days=max(1,min(days,365))
    clauses=[]; values=[]
    clauses.append("date(date_posted) >= date('now', ?)")
    values.append(f"-{days} days")
    if q:
        clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
        values += [f"%{q}%"]*3
    if location and location.strip().lower() not in {
        "worldwide", "global", "anywhere", "remote worldwide", "remote anywhere"
    }:
        location_term=location.split(",")[0].strip()
        clauses.append("(location LIKE ? OR search_location LIKE ?)")
        values.extend([f"%{location_term}%", f"%{location_term}%"])
    if source:
        source_values = [value.strip().lower() for value in source.split(",") if value.strip()]
        if source_values:
            placeholders = ",".join("?" for _ in source_values)
            clauses.append(f"lower(source) IN ({placeholders})")
            values.extend(source_values)
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with connect() as db:
        total=db.execute("SELECT COUNT(*) FROM jobs"+where,values).fetchone()[0]
        rows=db.execute("SELECT * FROM jobs"+where+" ORDER BY COALESCE(date_posted,'') DESC, id DESC LIMIT ? OFFSET ?",values+[limit,offset]).fetchall()
        return {"total":total,"items":[dict(r) for r in rows]}

@app.get("/api/searches")
def searches():
    with connect() as db:
        rows=db.execute("SELECT * FROM searches ORDER BY id DESC LIMIT 20").fetchall()
        return [dict(r) for r in rows]

@app.get("/api/export.xlsx")
def export_excel():
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    headers = ["Title", "Company", "Location", "Source", "Date posted",
               "Job URL", "Experience", "Skills"]
    with connect() as db:
        rows = db.execute(
            """SELECT title,company,location,source,date_posted,job_url,
                      experience_range,skills
               FROM jobs
               WHERE date(date_posted) >= date('now', '-30 days')
               ORDER BY id DESC"""
        ).fetchall()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Job Listings"
    sheet.append(headers)
    for row in rows:
        values = list(row)
        sheet.append(values)
        url_cell = sheet.cell(row=sheet.max_row, column=6)
        if url_cell.value:
            url_cell.hyperlink = str(url_cell.value)
            url_cell.style = "Hyperlink"

    header_fill = PatternFill("solid", fgColor="7A2432")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = [30, 24, 24, 16, 18, 48, 22, 36]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 24

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="jobpilot_jobs.xlsx"'}
    )
