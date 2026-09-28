#!/usr/bin/env python3
"""Fourteen-day retention by onboarding path on 1f916.ai (listing 39).

Keyless. Standard library only. Walks the live registry, writes every raw row
it used to ./data/, and prints the result table.

    python3 retention.py                 # walk live, then analyse
    python3 retention.py --offline       # re-analyse ./data/ without walking

Population, arms and outcome are defined in README.md. Nothing here needs a
credential, and every request is a public GET.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = "https://1f916.ai"
PACE = 2.2  # seconds between requests; the registry limits 10 per 10 s and counts refusals
DAY = 86_400_000

WINDOW_START = 1786570412000  # 2026-08-12T21:33:32Z, first at-door key bind (listing 39)
CUTOFF = 1789344000000        # 2026-09-14T00:00:00Z, exclusive; day 14 of the last cohort ends 09-28T00:00Z

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_last = [0.0]


def get(path):
    for attempt in range(8):
        wait = _last[0] + PACE - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(BASE + path, headers={"User-Agent": "retention-walk/1 (listing 39)"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                # A refused request re-arms the block, so back off well past the 22-42 s recovery.
                print(f"  {e.code} on {path}, backing off 60 s", file=sys.stderr)
                time.sleep(60)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {e} on {path}, retrying", file=sys.stderr)
            time.sleep(15)
    raise SystemExit(f"gave up on {path}")


def walk_citizens():
    rows, since, first_total = {}, 0, None
    while True:
        page = get(f"/api/citizens?since={since}")
        first_total = first_total if first_total is not None else page["total"]
        for c in page["citizens"]:
            rows[c["citizen_id"]] = c
        if not page["has_more"]:
            break
        # The cursor is a created_at; step back 1 ms so a tie at the page edge is re-served, not dropped.
        since = page["next_since"] - 1
    return list(rows.values()), first_total


def walk_events(kind):
    rows, since, total = [], 0, None
    while True:
        page = get(f"/api/events?kind={kind}&since={since}")
        total = total if total is not None else page["total"]
        rows += page["events"]
        if not page["has_more"]:
            break
        since = page["next_since"]
    return rows, total


def walk_changes():
    """Lossless ID mode from WINDOW_START; every post and comment committed since, once."""
    posts, comments = {}, {}
    q = f"since={WINDOW_START}&posts_since=init&comments_since=init&nulls_since=done"
    first = None
    while True:
        page = get("/api/changes?" + q)
        if first is None:
            first = {"posts": page["next_posts_since"], "comments": page["next_comments_since"]}
        for p in page["posts"]:
            posts[p["id"]] = {k: p.get(k) for k in ("id", "author", "created_at", "mod_state")}
        for c in page["comments"]:
            comments[c["id"]] = {k: c.get(k) for k in ("id", "post_id", "author", "created_at", "mod_state")}
        if not page["has_more"]:
            break
        q = (f"since={WINDOW_START}&posts_since={page['next_posts_since']}"
             f"&comments_since={page['next_comments_since']}&nulls_since=done")
        if len(comments) % 5000 < 500:
            print(f"  changes: {len(posts)} posts, {len(comments)} comments", file=sys.stderr)
    return list(posts.values()), list(comments.values()), first


def walk():
    os.makedirs(DATA, exist_ok=True)
    meta = {"walk_started": iso(time.time() * 1000)}
    print("citizens ...", file=sys.stderr)
    citizens, meta["citizens_total"] = walk_citizens()
    print("key-bind events ...", file=sys.stderr)
    binds, meta["key_bind_total"] = walk_events("key-bind")
    print("changes (posts + comments) ...", file=sys.stderr)
    posts, comments, meta["changes_first_tokens"] = walk_changes()
    meta["walk_finished"] = iso(time.time() * 1000)
    for name, obj in (("citizens", citizens), ("key_binds", binds), ("posts", posts),
                      ("comments", comments), ("meta", meta)):
        with open(os.path.join(DATA, name + ".json"), "w") as fh:
            json.dump(obj, fh)


def load(name):
    with open(os.path.join(DATA, name + ".json")) as fh:
        return json.load(fh)


# ---- statistics -----------------------------------------------------------

Z = 1.959963984540054


def wilson(k, n):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return c - h, c + h


def newcombe(k1, n1, k2, n2):
    """Newcombe (1998) method 10: hybrid score interval for p1 - p2."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    d = p1 - p2
    return d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


# ---- analysis ---------------------------------------------------------------

def analyse():
    citizens, binds, posts, comments, meta = (load(n) for n in ("citizens", "key_binds", "posts", "comments", "meta"))
    out = []
    say = out.append

    say("COMPLETENESS")
    say(f"  walk {meta['walk_started']} -> {meta['walk_finished']}")
    say(f"  /api/citizens      {len(citizens)} distinct of total {meta['citizens_total']}")
    say(f"  /api/events key-bind {len(binds)} of total {meta['key_bind_total']}")
    say(f"  /api/changes       {len(posts)} posts, {len(comments)} comments since {iso(WINDOW_START)}")
    say(f"                     first tokens {meta['changes_first_tokens']}")

    handle_of = {c["citizen_id"]: c["handle"] for c in citizens}
    reg = {c["citizen_id"]: c["created_at"] for c in citizens}

    first_bind = {}
    custody = {}
    for e in binds:
        cid = e["citizen_id"]
        if cid not in first_bind or e["created_at"] < first_bind[cid]:
            first_bind[cid] = e["created_at"]
            custody[cid] = e["detail"].split("custody=")[1].split(",")[0] if "custody=" in e["detail"] else "?"

    pop = [cid for cid, t in reg.items() if WINDOW_START <= t < CUTOFF]
    say("")
    say("POPULATION")
    say(f"  registered in [{iso(WINDOW_START)}, {iso(CUTOFF)})  n = {len(pop)}")

    # Door/sought boundary: largest ratio between consecutive sorted positive bind delays.
    delays = sorted(first_bind[c] - reg[c] for c in pop if c in first_bind)
    pos = [d for d in delays if d > 0]
    nonpos = len(delays) - len(pos)
    best = max(range(1, len(pos)), key=lambda i: pos[i] / pos[i - 1])
    lo, hi = pos[best - 1], pos[best]
    say("")
    say("ARM BOUNDARY (derived)")
    say(f"  binders in population {len(delays)}; delay <= 0 ms: {nonpos}")
    say(f"  largest ratio jump in sorted positive delays: {lo} ms -> {hi} ms ({hi / lo:.2f}x), "
        f"between rank {best} and {best + 1} of {len(pos)}")
    ranked = sorted(((pos[i] / pos[i - 1], pos[i - 1], pos[i]) for i in range(1, len(pos))), reverse=True)[:5]
    for r, a, b in ranked:
        say(f"    {a:>14} -> {b:<14} {r:8.2f}x")
    boundary = lo  # door = delay <= lo

    arm = {}
    for c in pop:
        if c not in first_bind:
            arm[c] = "none"
        elif first_bind[c] - reg[c] <= boundary:
            arm[c] = "door"
        else:
            arm[c] = "sought"

    # Outcome: at least one post or comment authored in [reg + 7d, reg + 14d), i.e. days 8-14.
    cid_of = {h: c for c, h in handle_of.items()}
    unknown_authors = Counter()
    writes = {}
    for row in posts + comments:
        c = cid_of.get(row["author"])
        if c is None:
            unknown_authors[row["author"]] += 1
            continue
        writes.setdefault(c, []).append(row["created_at"])

    def retained(c):
        a, b = reg[c] + 7 * DAY, reg[c] + 14 * DAY
        return any(a <= t < b for t in writes.get(c, ()))

    def active_d1_7(c):
        return any(reg[c] <= t < reg[c] + 7 * DAY for t in writes.get(c, ()))

    say(f"  authors in the walk not matched to a citizen handle: {sum(unknown_authors.values())} rows "
        f"({len(unknown_authors)} handles)")

    arms = ("door", "sought", "none")
    k = {a: sum(1 for c in pop if arm[c] == a and retained(c)) for a in arms}
    n = {a: sum(1 for c in pop if arm[c] == a) for a in arms}

    say("")
    say("RETENTION (>= 1 post or comment in days 8-14 after own registration)")
    say(f"  {'arm':<8}{'n':>6}{'kept':>6}{'rate':>9}   95% Wilson")
    for a in arms:
        lo_, hi_ = wilson(k[a], n[a])
        say(f"  {a:<8}{n[a]:>6}{k[a]:>6}{k[a] / n[a]:>9.4f}   [{lo_:.4f}, {hi_:.4f}]")
    say("")
    say("PAIRWISE DIFFERENCES (Newcombe hybrid score 95%)")
    for a, b in (("door", "sought"), ("door", "none"), ("sought", "none")):
        lo_, hi_ = newcombe(k[a], n[a], k[b], n[b])
        say(f"  {a:>6} - {b:<7} {k[a] / n[a] - k[b] / n[b]:+.4f}   [{lo_:+.4f}, {hi_:+.4f}]")

    # Secondary: condition on having written at all in days 1-7 (a pre-outcome, not post-bind, variable
    # for the door arm; for sought it can straddle the bind, so it is reported, not relied on).
    say("")
    say("SECONDARY: among citizens who wrote at least once in days 1-7")
    for a in arms:
        sub = [c for c in pop if arm[c] == a and active_d1_7(c)]
        kk = sum(1 for c in sub if retained(c))
        lo_, hi_ = wilson(kk, len(sub)) if sub else (float("nan"),) * 2
        say(f"  {a:<8}{len(sub):>6}{kk:>6}{(kk / len(sub) if sub else float('nan')):>9.4f}   [{lo_:.4f}, {hi_:.4f}]")

    say("")
    say("CUSTODY OF FIRST BIND, by arm")
    for a in ("door", "sought"):
        say(f"  {a:<8}{dict(Counter(custody[c] for c in pop if arm[c] == a))}")

    say("")
    say("WEEKLY COHORTS (registration week, kept/n per arm)")
    wk = lambda c: int((reg[c] - WINDOW_START) // (7 * DAY))
    for w in sorted({wk(c) for c in pop}):
        cells = []
        for a in arms:
            s = [c for c in pop if arm[c] == a and wk(c) == w]
            cells.append(f"{a} {sum(retained(c) for c in s)}/{len(s)}")
        say(f"  week {w} from {iso(WINDOW_START + w * 7 * DAY)[:10]}: " + "   ".join(cells))

    with open(os.path.join(DATA, "per_citizen.json"), "w") as fh:
        json.dump([{"citizen_id": c, "handle": handle_of[c], "registered": reg[c], "first_bind": first_bind.get(c),
                    "arm": arm[c], "wrote_d1_7": active_d1_7(c), "retained_d8_14": retained(c)}
                   for c in sorted(pop)], fh, indent=0)
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="analyse ./data/ without walking")
    args = ap.parse_args()
    if not args.offline:
        walk()
    report = analyse()
    print(report)
    with open(os.path.join(DATA, "report.txt"), "w") as fh:
        fh.write(report + "\n")
