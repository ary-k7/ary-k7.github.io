#!/usr/bin/env python3
"""Build markets.json for the Markets page. Standard library only.

    python3 tools/fetch_markets.py --out markets.json [--previous old-markets.json]
                                   [--config src/data/markets-config.json]

Reads the instruments and calendar settings from src/data/markets-config.json (the page reads
the same file). One request to TradingView's public quote feed returns the last price, the day's
change, the week's change and the 52-week range for every instrument; a second request returns
the economic calendar. Neither needs a key.

The quote feed has no price history, so each instrument's daily history is carried over from
--previous and today's value is added to it on every run. tools/seed_history.mjs fills in the
past year once. If the quotes or the calendar fail, that part is copied from --previous and a
note goes into "warnings". The output is only written when something usable was fetched; the
exit code is non-zero only if nothing was.
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.error, urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ORIGIN = "https://in.tradingview.com"
COLUMNS = ["close", "change", "change_abs", "Perf.W", "price_52_week_high", "price_52_week_low"]
HISTORY_DAYS = 370
warnings = []


def get(url, headers=None, data=None, timeout=30, tries=3, backoff=(5, 20, 60)):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return fh.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - report and retry
            last = e
            if i < tries - 1:
                time.sleep(backoff[i])
    raise RuntimeError(f"{url}: {last}")


def r(x, nd):
    return None if x is None else round(x, nd)


def fetch_quotes(tickers):
    body = json.dumps({"symbols": {"tickers": tickers}, "columns": COLUMNS}).encode()
    raw = get("https://scanner.tradingview.com/global/scan", data=body,
              headers={"Content-Type": "application/json", "Origin": ORIGIN})
    out = {}
    for item in json.loads(raw).get("data", []):
        out[item["s"]] = dict(zip(COLUMNS, item["d"]))
    return out


def add_today(history, today, value, nd):
    """Carry the history forward: replace today's point or append it, and keep about a year."""
    hist = [h for h in (history or []) if h[0] != today]
    weekend = dt.date.fromisoformat(today).weekday() >= 5
    # At weekends most markets are shut: only add a point if the value actually moved.
    if not (weekend and hist and hist[-1][1] == r(value, nd)):
        hist.append([today, r(value, nd)])
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    return [h for h in hist if h[0] >= cutoff]


def quote_row(spec, q, prev_row, today):
    unit = spec.get("unit", "price")
    last = q["close"]
    if last is None:
        raise RuntimeError("no last price")
    if unit == "yield":
        nd = 3
        chg1d = None if q["change_abs"] is None else q["change_abs"] * 100          # basis points
        # The feed gives the week's move as a percentage of the yield; turn it into basis points.
        chg1w = None if q["Perf.W"] is None else (last - last / (1 + q["Perf.W"] / 100)) * 100
    else:
        nd = 4 if abs(last) < 20 else 2
        chg1d, chg1w = q["change"], q["Perf.W"]
    return {
        "id": spec["id"], "name": spec["name"], "unit": unit,
        "last": r(last, nd), "chg1d": r(chg1d, 2), "chg1w": r(chg1w, 2),
        "hi52": r(q["price_52_week_high"], nd), "lo52": r(q["price_52_week_low"], nd),
        "asOf": today, "freq": "daily",
        "history": add_today((prev_row or {}).get("history"), today, last, nd),
    }


def spread_row(spec, done, prev_row, today):
    """Long yield minus short yield, in basis points."""
    a, b = done[spec["source"]["long"]], done[spec["source"]["short"]]
    last = (a["last"] - b["last"]) * 100
    def diff(key):
        return None if a[key] is None or b[key] is None else a[key] - b[key]
    hist = add_today((prev_row or {}).get("history"), today, last, 1)
    vals = [v for _, v in hist]
    return {
        "id": spec["id"], "name": spec["name"], "unit": "bp",
        "last": r(last, 1), "chg1d": r(diff("chg1d"), 1), "chg1w": r(diff("chg1w"), 1),
        # The 52-week range of a spread comes from its own history, once that has been seeded.
        "hi52": r(max(vals), 1) if len(vals) >= 200 else None,
        "lo52": r(min(vals), 1) if len(vals) >= 200 else None,
        "asOf": today, "freq": "daily", "history": hist,
    }


def fetch_calendar(cfg):
    """Events from daysBack ago to daysForward ahead, asked for a month at a time.

    High-importance events and anything a preset looks for are kept for the whole range.
    Medium-importance events are kept only near today (mediumBack / mediumForward days),
    which keeps the file small.
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=cfg["daysBack"])).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + dt.timedelta(days=cfg["daysForward"])).replace(hour=23, minute=59, second=59, microsecond=0)
    med_from = now - dt.timedelta(days=cfg.get("mediumBack", 28))
    med_to = now + dt.timedelta(days=cfg.get("mediumForward", 14))
    codes = ",".join(c["code"] for c in cfg["countries"])
    pres = [(p["country"], re.compile(p["match"], re.I)) for p in cfg["presets"]]
    seen, out = set(), []
    lo = start
    while lo < end:
        hi = min(lo + dt.timedelta(days=31), end)
        url = ("https://economic-calendar.tradingview.com/events?from=" + lo.strftime("%Y-%m-%dT%H:%M:%S.000Z")
               + "&to=" + hi.strftime("%Y-%m-%dT%H:%M:%S.000Z") + "&countries=" + codes)
        data = json.loads(get(url, headers={"Origin": ORIGIN}))
        if data.get("status") != "ok":
            raise RuntimeError("calendar status " + str(data.get("status")))
        for e in data["result"]:
            if e["id"] in seen:
                continue
            when = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            imp = e.get("importance", -1)
            wanted = any(e["country"] == c and rx.search(e["title"]) for c, rx in pres)
            if not (imp >= 1 or wanted or (imp >= 0 and med_from <= when <= med_to)):
                continue
            seen.add(e["id"])
            out.append({
                "id": e["id"], "time": e["date"].replace(".000Z", "Z"), "country": e["country"],
                "title": e["title"], "importance": imp if imp >= 0 else 0,
                "actual": e.get("actual"), "forecast": e.get("forecast"), "previous": e.get("previous"),
                "unit": e.get("unit") or "", "scale": e.get("scale") or "", "period": e.get("period") or "",
            })
        lo = hi
        time.sleep(0.5)
    out.sort(key=lambda x: (x["time"], x["country"], x["title"]))
    return out



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--previous")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--config", default=os.path.join(here, "..", "src", "data", "markets-config.json"))
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    prev = {}
    if a.previous and os.path.exists(a.previous):
        try:
            prev = json.load(open(a.previous))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"previous file unreadable: {e}")
    prev_rows = {r_["id"]: r_ for b in prev.get("boards", []) for r_ in b["rows"]}
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    specs = [s_ for b in cfg["boards"] for s_ in b["rows"]]

    fetched, quotes = 0, {}
    try:
        quotes = fetch_quotes([s_["source"]["ticker"] for s_ in specs if s_["source"]["type"] == "tv"])
        if not quotes:
            raise RuntimeError("quote feed came back empty")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"quotes: {e}")

    done = {}
    for spec in sorted(specs, key=lambda s_: s_["source"]["type"] == "spread"):   # spreads last
        p_ = prev_rows.get(spec["id"])
        try:
            if spec["source"]["type"] == "spread":
                row = spread_row(spec, done, p_, today)
            else:
                q = quotes.get(spec["source"]["ticker"])
                if q is None:
                    raise RuntimeError(f"no quote for {spec['source']['ticker']}")
                row = quote_row(spec, q, p_, today)
            fetched += 1
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{spec['id']}: {e}")
            if not p_:
                continue
            row = {**p_, "name": spec["name"], "stale": True}
            warnings.append(f"{spec['id']}: kept previous values (as of {row['asOf']})")
        done[spec["id"]] = row
    boards = [{"id": b["id"], "title": b["title"], "note": b.get("note", ""),
               "rows": [done[x["id"]] for x in b["rows"] if x["id"] in done]} for b in cfg["boards"]]

    try:
        calendar = fetch_calendar(cfg["calendar"])
        if not calendar:
            raise RuntimeError("calendar came back empty")
        fetched += 1
    except Exception as e:  # noqa: BLE001
        warnings.append(f"calendar: {e}")
        calendar = prev.get("calendar", [])
        if calendar:
            warnings.append("calendar: kept previous events")

    for w in warnings:
        print("warning:", w, file=sys.stderr)
    if fetched == 0:
        print("nothing could be fetched; not writing", file=sys.stderr)
        return 1
    result = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warnings": warnings, "boards": boards, "calendar": calendar,
    }
    tmp = a.out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, a.out)
    print(f"wrote {a.out}: {sum(len(b['rows']) for b in boards)} rows, {len(calendar)} events, "
          f"{os.path.getsize(a.out) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
