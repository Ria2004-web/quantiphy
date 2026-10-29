
import os
import re
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="DevPulse API")
BASE = Path(__file__).parent
DB = BASE / "devpulse.db"
TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "DevPulse-Dashboard"
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

SKILLS = {
    "python": ["python", "py"],
    "javascript": ["javascript", "js"],
    "typescript": ["typescript", "ts"],
    "java": ["java"],
    "c++": ["c++", "cpp"],
    "c#": ["c#", "csharp"],
    "go": ["golang", "go"],
    "rust": ["rust"],
    "ruby": ["ruby"],
    "php": ["php"],
    "html": ["html"],
    "css": ["css"],
    "sql": ["sql"],
    "react": ["react", "reactjs", "react.js"],
    "node.js": ["node", "nodejs", "node.js"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "docker": ["docker"],
    "aws": ["aws"],
    "git": ["git"],
    "mongodb": ["mongodb"],
    "postgresql": ["postgresql", "postgres"],
    "sqlite": ["sqlite"],
}

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            username TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

init_db()

def github_get(url, params=None):
    try:
        r = requests.get(
            url, headers=HEADERS,
            params=params, timeout=15
        )
        if r.status_code == 404:
            raise HTTPException(404, "GitHub user not found")
        if r.status_code in (403, 429):
            raise HTTPException(
                503, "GitHub API rate limit reached. Try later."
            )
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        raise HTTPException(502, "Unable to contact GitHub")

def get_contributions(username):
    if not TOKEN:
        return None

    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """

    try:
        r = requests.post(
            "https://api.github.com/graphql",
            headers=HEADERS,
            json={
                "query": query,
                "variables": {"login": username}
            },
            timeout=15
        )
        r.raise_for_status()
        result = r.json()
        return result["data"]["user"][
            "contributionsCollection"
        ]["contributionCalendar"]
    except (KeyError, TypeError, requests.RequestException):
        return None

def calculate_streak(days):
    active = {
        d["date"] for d in days
        if d["contributionCount"] > 0
    }
    if not active:
        return {"current": 0, "longest": 0}

    ordered = sorted(active)
    longest = 0
    running = 0
    previous = None

    for date in ordered:
        current = datetime.strptime(date, "%Y-%m-%d").date()
        if previous and (current - previous).days == 1:
            running += 1
        else:
            running = 1
        longest = max(longest, running)
        previous = current

    today = datetime.now(timezone.utc).date()
    current = 0
    cursor = today

    if cursor.isoformat() not in active:
        cursor -= timedelta(days=1)

    while cursor.isoformat() in active:
        current += 1
        cursor -= timedelta(days=1)

    return {"current": current, "longest": longest}

def build_profile(username):
    user = github_get(
        f"https://api.github.com/users/{username}"
    )

    repos = []
    page = 1

    while page <= 10:
        batch = github_get(
            f"https://api.github.com/users/{username}/repos",
            {
                "per_page": 100,
                "page": page,
                "type": "owner"
            }
        )
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    repositories = []
    languages = Counter()

    for repo in repos:
        if repo.get("fork"):
            continue

        language = repo.get("language") or "Unknown"
        if language != "Unknown":
            languages[language] += 1

        repositories.append({
            "name": repo["name"],
            "description": repo.get("description") or
                           "No description provided",
            "language": language,
            "stars": repo["stargazers_count"],
            "forks": repo["forks_count"],
            "url": repo["html_url"],
            "updated": repo["updated_at"],
            "topics": repo.get("topics", [])
        })

    calendar = get_contributions(username)
    days = []

    if calendar:
        for week in calendar["weeks"]:
            days.extend(week["contributionDays"])

    return {
        "user": {
            "name": user.get("name") or user["login"],
            "login": user["login"],
            "avatar": user["avatar_url"],
            "bio": user.get("bio") or "GitHub Developer",
            "followers": user["followers"],
            "public_repos": user["public_repos"],
            "url": user["html_url"]
        },
        "repositories": repositories,
        "total_stars": sum(r["stars"] for r in repositories),
        "languages": dict(languages),
        "contributions": days,
        "contribution_total": (
            calendar["totalContributions"] if calendar else None
        ),
        "streak": calculate_streak(days),
        "contribution_available": calendar is not None
    }

def profile_data(username, refresh=False):
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", username):
        raise HTTPException(400, "Invalid GitHub username")

    with connect() as db:
        row = db.execute(
            "SELECT * FROM cache WHERE username=?",
            (username.lower(),)
        ).fetchone()

    if row and not refresh:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            row["updated_at"]
        )
        if age < timedelta(minutes=30):
            return json.loads(row["payload"])

    try:
        data = build_profile(username)
    except HTTPException:
        if row:
            cached = json.loads(row["payload"])
            cached["stale"] = True
            return cached
        raise

    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
            (
                username.lower(),
                json.dumps(data),
                datetime.now(timezone.utc).isoformat()
            )
        )
    return data

class JobRequest(BaseModel):
    username: str
    description: str

class RepoRequest(BaseModel):
    username: str
    repository: str

class ChatRequest(BaseModel):
    username: str
    question: str

def extract_skills(text):
    text = text.lower()
    found = set()

    for skill, aliases in SKILLS.items():
        for alias in aliases:
            pattern = (
                r"(?<![a-z0-9])" +
                re.escape(alias) +
                r"(?![a-z0-9])"
            )
            if re.search(pattern, text):
                found.add(skill)
                break

    return found

@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/profile/{username}")
def profile(username: str, refresh: bool = False):
    return profile_data(username, refresh)

@app.post("/api/match")
def match_job(req: JobRequest):
    data = profile_data(req.username)
    required = extract_skills(req.description)

    available = set()
    for repo in data["repositories"]:
        available |= extract_skills(repo["language"])
        available |= extract_skills(" ".join(repo["topics"]))

    matched = required & available
    missing = required - available

    return {
        "score": round(len(matched) / len(required) * 100)
                 if required else None,
        "matched": sorted(matched),
        "missing": sorted(missing),
        "required": sorted(required),
        "note": "Based on repository languages and topics, "
                "not verified developer proficiency."
    }

@app.post("/api/chat")
def portfolio_chat(req: ChatRequest):
    data = profile_data(req.username)
    q = req.question.lower()
    repos = data["repositories"]
    langs = data["languages"]

    if not repos:
        answer = "This profile has no public owned repositories."
    elif "language" in q or "skill" in q:
        top = sorted(langs.items(), key=lambda x: -x[1])
        answer = "Your most-used repository languages are: " + (
            ", ".join(f"{k} ({v} repos)" for k, v in top[:5])
            if top else "No language data available."
        )
    elif "interview" in q or "highlight" in q or "best" in q:
        best = max(repos, key=lambda r: r["stars"])
        answer = (
            f"Consider highlighting {best['name']}. "
            f"It has {best['stars']} stars and uses "
            f"{best['language']}. {best['description']}"
        )
    elif "improve" in q or "suggest" in q:
        incomplete = [
            r["name"] for r in repos
            if r["description"] == "No description provided"
        ]
        answer = (
            "Add clear READMEs, screenshots and project demos. "
            + (
                "Start by adding descriptions to: " +
                ", ".join(incomplete[:4])
                if incomplete else
                "Keep your best repositories pinned on GitHub."
            )
        )
    else:
        answer = (
            f"{data['user']['name']} has {len(repos)} public owned "
            f"repositories and {data['total_stars']} total stars. "
            "Ask me about languages, interview projects or "
            "portfolio improvements."
        )

    return {"answer": answer}

@app.post("/api/readme")
def generate_readme(req: RepoRequest):
    data = profile_data(req.username)
    repo = next(
        (r for r in data["repositories"]
         if r["name"] == req.repository),
        None
    )

    if not repo:
        raise HTTPException(404, "Repository not found")

    content = f"""# {repo['name']}

## Overview
{repo['description']}

## Technology
- Primary language: {repo['language']}
- Topics: {', '.join(repo['topics']) or 'Not specified'}

## Repository
{repo['url']}

## Suggested README Improvements
- Add installation instructions after verifying the setup.
- Add screenshots or a demo link.
- Document the main features and usage examples.
- Explain how others can contribute.

> This README is generated from public repository metadata.
> Verify all information before publishing.
"""
    return {"readme": content}
