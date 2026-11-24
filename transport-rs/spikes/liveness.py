#!/usr/bin/env python3
"""Liveness evidence from the unauthenticated GitHub API (60 requests/hour per IP) for the WebRTC
library spike (DEV-764 S1). Python 3 stdlib only. Usage: liveness.py owner/repo [owner/repo ...]
Prints one JSON object per repo. Counts requests it makes and stops politely on a rate limit."""
import datetime as dt
import json
import statistics
import sys
import urllib.error
import urllib.request

UA = {"User-Agent": "spike-liveness-check", "Accept": "application/vnd.github+json"}
calls = 0


def get(url):
    global calls
    calls += 1
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read()[:200].decode("utf8", "replace")}, e.code


def parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    for repo in sys.argv[1:]:
        out = {"repo": repo}
        meta, _ = get(f"https://api.github.com/repos/{repo}")
        out["open_issues_and_prs"] = meta.get("open_issues_count")
        out["pushed_at"] = meta.get("pushed_at")
        out["stars"] = meta.get("stargazers_count")
        rel, _ = get(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        if isinstance(rel, list):
            cut = now - dt.timedelta(days=365)
            rs = [(r["tag_name"], r["published_at"][:10]) for r in rel if r.get("published_at") and parse(r["published_at"]) >= cut]
            out["releases_last_12m"] = len(rs)
            out["releases"] = rs[:40]
        else:
            out["releases_error"] = rel
        # commits in the last 6 months: authors and per-month counts
        since = (now - dt.timedelta(days=183)).strftime("%Y-%m-%dT%H:%M:%SZ")
        authors, months, n, page = {}, {}, 0, 1
        while page <= 6:
            cs, st = get(f"https://api.github.com/repos/{repo}/commits?since={since}&per_page=100&page={page}")
            if not isinstance(cs, list) or not cs:
                break
            for c in cs:
                n += 1
                a = (c.get("author") or {}).get("login") or c["commit"]["author"]["email"]
                authors[a] = authors.get(a, 0) + 1
                m = c["commit"]["committer"]["date"][:7]
                months[m] = months.get(m, 0) + 1
            if len(cs) < 100:
                break
            page += 1
        out["commits_last_6m"] = n
        out["commits_per_month"] = dict(sorted(months.items()))
        out["distinct_authors_6m"] = len(authors)
        out["top_authors_6m"] = sorted(authors.items(), key=lambda x: -x[1])[:6]
        # the 20 most recent issues (not PRs) and the first response by someone other than the author
        iss, _ = get(f"https://api.github.com/repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=60")
        issues = [i for i in iss if isinstance(iss, list) and "pull_request" not in i][:20] if isinstance(iss, list) else []
        com = []
        for page in (1, 2, 3):
            cs, _ = get(f"https://api.github.com/repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100&page={page}")
            if not isinstance(cs, list) or not cs:
                break
            com += cs
        by_issue = {}
        for c in com:
            num = int(c["issue_url"].rsplit("/", 1)[1])
            by_issue.setdefault(num, []).append(c)
        deltas, none = [], 0
        rows = []
        for i in issues:
            created = parse(i["created_at"])
            first = None
            for c in sorted(by_issue.get(i["number"], []), key=lambda c: c["created_at"]):
                if c["user"]["login"] != i["user"]["login"] and c["author_association"] in ("OWNER", "MEMBER", "COLLABORATOR") and c["user"]["type"] != "Bot":
                    first = parse(c["created_at"])
                    break
            if first:
                d = (first - created).total_seconds() / 3600
                deltas.append(d)
                rows.append((i["number"], i["created_at"][:10], round(d, 1)))
            else:
                none += 1
                rows.append((i["number"], i["created_at"][:10], None))
        out["recent_issues_checked"] = len(issues)
        out["issues_with_maintainer_response"] = len(deltas)
        out["issues_without_maintainer_response_in_window"] = none
        out["median_hours_to_first_maintainer_response"] = round(statistics.median(deltas), 1) if deltas else None
        out["issue_rows(number,created,hours)"] = rows
        sec, _ = get(f"https://api.github.com/repos/{repo}/security-advisories?per_page=30")
        out["published_security_advisories"] = (len(sec) if isinstance(sec, list) else sec)
        out["api_calls_so_far"] = calls
        print(json.dumps(out, indent=1))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
