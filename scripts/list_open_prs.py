#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

API_BASE = "https://api.github.com"
UA = "Compass-PR-Reporter/1.0 (+https://github.com/macmanishkr20/Compass)"


def http_get(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read().decode(charset)
        return json.loads(data)


def parse_repo_from_url(url: str) -> Tuple[str, str]:
    # Supports https and ssh forms
    # https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    # git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Unrecognized remote URL: {url}")


def shell(cmd: str) -> str:
    import subprocess
    out = subprocess.check_output(cmd, shell=True, text=True)
    return out


def get_connected_repos() -> List[Tuple[str, str]]:
    remotes_raw = shell("git remote -v")
    repos = []
    seen = set()
    for line in remotes_raw.splitlines():
        if "(fetch)" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1]
        try:
            owner, repo = parse_repo_from_url(url)
        except Exception:
            continue
        key = f"{owner}/{repo}"
        if key not in seen:
            seen.add(key)
            repos.append((owner, repo))
    return repos


def iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_age(created_at: str) -> Tuple[float, str]:
    created = iso_to_dt(created_at)
    now = datetime.now(timezone.utc)
    delta = now - created
    seconds = delta.total_seconds()
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    if days > 0:
        return seconds / 86400.0, f"{days}d {hours}h"
    elif hours > 0:
        return seconds / 86400.0, f"{hours}h {mins}m"
    else:
        return seconds / 86400.0, f"{mins}m"


def determine_review_status(owner: str, repo: str, number: int, pr: Dict[str, Any]) -> str:
    # If draft, treat as Draft
    if pr.get("draft"):
        return "Draft"
    # If there are requested reviewers, mark as "Review requested" unless already approved
    reviews = http_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}/reviews")
    # Build by reviewer latest state timestamp
    latest_by_user: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        user = (r.get("user") or {}).get("login") or ""
        submitted_at = r.get("submitted_at") or r.get("submittedAt") or r.get("submitted-at")
        try:
            ts = iso_to_dt(submitted_at) if submitted_at else None
        except Exception:
            ts = None
        prev = latest_by_user.get(user)
        if prev is None or (ts and prev.get("_ts") and ts > prev["_ts"]):
            latest_by_user[user] = {"state": r.get("state"), "_ts": ts}
    # Aggregate: if any latest state is CHANGES_REQUESTED, that's the status
    any_changes = any((v.get("state") == "CHANGES_REQUESTED") for v in latest_by_user.values())
    any_approved = any((v.get("state") == "APPROVED") for v in latest_by_user.values())
    if any_changes:
        return "Changes requested"
    if any_approved:
        return "Approved"
    # If there are review requests pending
    requested = pr.get("requested_reviewers") or []
    requested_teams = pr.get("requested_teams") or []
    if requested or requested_teams:
        return "Review requested"
    # If no reviews
    if not reviews:
        return "No reviews"
    return "Under review"


def determine_ci_status(owner: str, repo: str, sha: str) -> str:
    # Checks API
    try:
        checks = http_get(f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}/check-runs")
    except urllib.error.HTTPError as e:
        checks = {"total_count": 0, "check_runs": []}
    runs = checks.get("check_runs", [])
    if runs:
        any_in_progress = any(r.get("status") != "completed" for r in runs)
        any_failed = any((r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required")) for r in runs if r.get("status") == "completed")
        all_success = all((r.get("status") == "completed" and r.get("conclusion") == "success") for r in runs)
        if any_in_progress:
            return "In progress"
        if any_failed:
            return "Failing"
        if all_success:
            return "Passing"
        # Fall through
    # Combined status
    try:
        status = http_get(f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}/status")
        state = status.get("state")
        if state == "success":
            return "Passing"
        if state == "failure" or state == "error":
            return "Failing"
        if state == "pending":
            return "In progress"
    except urllib.error.HTTPError:
        pass
    return "No checks"


def main():
    repos = get_connected_repos()
    if not repos:
        print("No connected GitHub repositories found.")
        return
    all_rows = []
    for owner, repo in repos:
        try:
            prs = http_get(f"{API_BASE}/repos/{owner}/{repo}/pulls?state=open&per_page=100")
        except urllib.error.HTTPError as e:
            print(f"Error fetching PRs for {owner}/{repo}: {e}")
            continue
        for pr in prs:
            number = pr.get("number")
            # Fetch detailed PR for mergeable_state
            try:
                pr_detailed = http_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}")
            except urllib.error.HTTPError:
                pr_detailed = pr
            mergeable_state = pr_detailed.get("mergeable_state") or pr.get("mergeable_state") or "unknown"
            has_conflicts = (mergeable_state == "dirty")
            review_status = determine_review_status(owner, repo, number, pr_detailed)
            ci_status = determine_ci_status(owner, repo, pr.get("head", {}).get("sha", ""))
            age_days_float, age_str = fmt_age(pr.get("created_at"))
            all_rows.append({
                "repo": f"{owner}/{repo}",
                "number": number,
                "title": pr.get("title"),
                "author": (pr.get("user") or {}).get("login"),
                "created_at": pr.get("created_at"),
                "age_days": age_days_float,
                "age_str": age_str,
                "review_status": review_status,
                "ci_status": ci_status,
                "merge_conflicts": has_conflicts,
                "draft": pr.get("draft", False),
                "html_url": pr.get("html_url"),
            })
    # Sort by oldest first (created_at ascending)
    all_rows.sort(key=lambda r: r["created_at"] or "")
    # Render Markdown table
    print("| Repo | PR | Author | Open for | Review status | CI status | Merge conflicts |")
    print("|---|---|---|---:|---|---|---|")
    for r in all_rows:
        warn = " ⚠️" if r["age_days"] > 3.0 else ""
        conflicts = "Yes" if r["merge_conflicts"] else "No"
        pr_link = f"[#${r['number']}]({r['html_url']}) {r['title']}" if r.get("html_url") else f"#{r['number']} {r['title']}"
        print(f"| {r['repo']} | {pr_link}{warn} | {r['author']} | {r['age_str']} | {r['review_status']} | {r['ci_status']} | {conflicts} |")


if __name__ == "__main__":
    main()
