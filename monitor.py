#!/usr/bin/env python3
"""전북교육청교직원수련원 예약현황 감시 -> 텔레그램 알림 (GitHub Actions / 내 PC 공용).

- 감시 조건: config.json 직접 수정 또는 텔레그램 명령(/watch, /list, /remove, /status)
- 알림: 마감/추첨제 -> 예약가능으로 바뀐 칸, 감시 날짜의 추첨 접수 시작
- 표준 라이브러리만 사용 (pip 설치 필요 없음)

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

  python3 monitor.py                  1회 실행 (GitHub Actions가 5분마다 호출)
  python3 monitor.py --loop           계속 실행 (내 PC용, 새벽 1~5시 제외, 기본 5분 간격)
  python3 monitor.py --test           텔레그램 + 사이트 접속 테스트
  python3 monitor.py --dry-run        텔레그램 대신 화면에 출력 (상태 저장 안 함, --save 로 저장)
"""
import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

URL = "https://office.jbedu.kr/jbetc/MABAEAD/list.do"
KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"
AVAILABLE, LOTTERY = "예약가능", "추첨제"

SPAN_RE = re.compile(r'<span\b[^>]*\bclass="fullcon\d*"[^>]*>', re.I)
TITLE_ATTR_RE = re.compile(r'\btitle="([^"]*)"')
TITLE_RE = re.compile(r"^(\d{1,2})월\s*(\d{1,2})일\s*-\s*(.+)\s+-\s+([^-]+?)\s*$")  # "09월 22일 - 301호 - 예약가능"

DATE_RE = re.compile(r"^(\d{1,2})(?:[-/.]|월)(\d{1,2})일?$")
MONTH_RE = re.compile(r"^(\d{1,2})월$")
WEEKDAY_RE = re.compile(r"^[월화수목금토일]+$")

DEFAULT_CONFIG = {"watches": [], "fail_alert_after": 6}

HELP = """감시 조건 명령어
/watch 10-09~10-10        날짜 범위 감시
/watch 11월 금토 6인실     11월 금·토, 6인실만
/watch 11-20 -302호        302호는 제외 (방 앞에 -)
/list      감시 목록 보기
/remove 2  2번 삭제  (/clear 는 전부 삭제)
/status    지금 바로 확인해서 상태 알려줌

날짜: 11-15, 11/15, 11월15일, 11-15~11-17, 11월(한 달 전체)
요일: 금토, 토 등 / 방: 6인실, 302호, 원룸 등 (여러 개 가능)"""


# ---------------------------------------------------------------- 가져오기/파싱
def fetch_html(url=URL, attempts=3, timeout=20):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"사이트 접속 실패: {last}")


def infer_date(month, day, ref):
    try:
        d = date(ref.year, month, day)
        return date(ref.year + 1, month, day) if d < ref - timedelta(days=60) else d
    except ValueError:
        return None


def parse_cells(page, today):
    """HTML -> {"YYYY-MM-DD|객실명": 상태}"""
    cells = {}
    for tag in SPAN_RE.findall(page):
        t = TITLE_ATTR_RE.search(tag)
        m = TITLE_RE.match(htmlmod.unescape(t.group(1))) if t else None
        if not m:
            continue
        d = infer_date(int(m.group(1)), int(m.group(2)), today)
        if d:
            cells[f"{d.isoformat()}|{m.group(3).strip()}"] = m.group(4).strip()
    return cells


def get_cells(args, today):
    if args.html_file:
        with open(args.html_file, encoding="utf-8") as f:
            page = f.read()
    else:
        page = fetch_html()
    cells = parse_cells(page, today)
    if not cells:
        raise RuntimeError("예약 표를 읽지 못했습니다. (접속 차단 또는 페이지 구조 변경)")
    return cells


# ---------------------------------------------------------------- 감시 조건 문법
def fmt_day(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def _single(tok, ref):
    m = DATE_RE.match(tok)
    if not m:
        raise ValueError(f"날짜를 읽지 못했어요: {tok}")
    mo, da = int(m.group(1)), int(m.group(2))
    try:
        d = date(ref.year, mo, da)
        return d if d >= ref else date(ref.year + 1, mo, da)
    except ValueError:
        raise ValueError(f"없는 날짜예요: {tok}") from None


def _month(m, ref):
    if not 1 <= m <= 12:
        raise ValueError(f"없는 달이에요: {m}월")
    y = ref.year if m >= ref.month else ref.year + 1
    end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return date(y, m, 1), end


def parse_watch_text(text, ref):
    """'11월 금토 6인실 -302호' -> {ranges, weekdays, include, exclude}"""
    t = re.sub(r"(\d{1,2})월\s+(\d{1,2})일", r"\1월\2일", text)
    t = re.sub(r"\s*~\s*", "~", t)
    ranges, weekdays, include, exclude = [], [], [], []
    last = None  # 직전에 읽은 날짜 ("10월 16일 17일" 처럼 '17일'만 오면 같은 달로 본다)
    for tok in (x for x in re.split(r"[\s,]+", t.strip()) if x):
        if last and re.fullmatch(r"\d{1,2}일", tok):
            try:
                last = last.replace(day=int(tok[:-1]))
            except ValueError:
                raise ValueError(f"없는 날짜예요: {tok}") from None
            ranges.append((last, last))
        elif "~" in tok:
            a, b = tok.split("~", 1)
            start = _single(a, ref)
            if re.fullmatch(r"\d{1,2}일?", b):
                try:
                    end = start.replace(day=int(b.rstrip("일")))
                except ValueError:
                    raise ValueError(f"없는 날짜예요: {tok}") from None
            else:
                end = _single(b, start)
            if end < start or (end - start).days > 120:
                raise ValueError(f"날짜 범위가 이상해요: {tok}")
            ranges.append((start, end))
            last = end
        elif MONTH_RE.match(tok):
            ranges.append(_month(int(tok[:-1]), ref))
            last = None
        elif DATE_RE.match(tok):
            last = _single(tok, ref)
            ranges.append((last, last))
        elif WEEKDAY_RE.match(tok):
            weekdays += [WEEKDAYS.index(c) for c in tok if WEEKDAYS.index(c) not in weekdays]
        elif tok.startswith("-") and len(tok) > 1:
            exclude.append(tok[1:])
        else:
            include.append(tok)
    if not ranges:
        raise ValueError("날짜가 없어요")
    return {"ranges": ranges, "weekdays": sorted(weekdays), "include": include, "exclude": exclude}


def describe(p):
    s = ", ".join(fmt_day(a) if a == b else f"{fmt_day(a)}~{fmt_day(b)}" for a, b in p["ranges"])
    if p["weekdays"]:
        s += " · " + "".join(WEEKDAYS[i] for i in p["weekdays"]) + "요일만"
    if p["include"]:
        s += " · " + ", ".join(p["include"])
    if p["exclude"]:
        s += " · 제외 " + ", ".join(p["exclude"])
    return s


def watch_matches(p, d, room):
    if not any(a <= d <= b for a, b in p["ranges"]):
        return False
    if p["weekdays"] and d.weekday() not in p["weekdays"]:
        return False
    if p["include"] and not any(s in room for s in p["include"]):
        return False
    return not any(s in room for s in p["exclude"])


def key_matches(w, key):
    d_s, room = key.split("|", 1)
    return watch_matches(w["p"], date.fromisoformat(d_s), room)


# ---------------------------------------------------------------- 설정/상태 파일
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(path, {}))
    if not isinstance(cfg.get("watches"), list):
        cfg["watches"] = []
    return cfg


def write_json(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def normalize(cfg, today):
    """직접 손으로 추가한 항목에 id/ref를 채워 준다."""
    dirty = False
    nxt = max([e["id"] for e in cfg["watches"] if isinstance(e.get("id"), int)] + [0]) + 1
    for e in cfg["watches"]:
        if not isinstance(e.get("id"), int):
            e["id"], nxt, dirty = nxt, nxt + 1, True
        if not e.get("ref"):
            e["ref"], dirty = today.isoformat(), True
    return dirty


def compile_watches(cfg, today):
    ok, bad = [], []
    for e in cfg["watches"]:
        try:
            p = parse_watch_text(str(e.get("text", "")), date.fromisoformat(e.get("ref") or today.isoformat()))
        except ValueError as ex:
            bad.append((e, str(ex)))
            continue
        sig = hashlib.sha1(f'{e["id"]}|{e.get("text")}|{e.get("ref")}'.encode()).hexdigest()[:10]
        ok.append({"id": e["id"], "text": e["text"], "p": p, "sig": sig})
    return ok, bad


def prune_expired(cfg, today):
    ok, _ = compile_watches(cfg, today)
    dead = [w for w in ok if max(b for _, b in w["p"]["ranges"]) < today]
    ids = {w["id"] for w in dead}
    if ids:
        cfg["watches"] = [e for e in cfg["watches"] if e["id"] not in ids]
    return dead


# ---------------------------------------------------------------- 텔레그램
def tg_call(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없습니다.")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"텔레그램 오류 (HTTP {e.code}): {e.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"텔레그램 서버에 연결하지 못했습니다: {e.reason}") from None
    if not body.get("ok"):
        raise RuntimeError(f"텔레그램 오류: {body}")
    return body["result"]


def send_telegram(text, dry_run=False):
    text = text[:3900]
    if dry_run:
        print("----- (dry-run) 텔레그램 메시지 -----")
        print(text)
        print("--------------------------------------")
        return
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID 환경변수가 없습니다.")
    tg_call("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": "true"})


def read_updates(args, offset):
    try:
        if args.updates_file:
            return load_json(args.updates_file, [])
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            return tg_call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": '["message"]'})
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 텔레그램 명령을 읽지 못했어요: {e}", file=sys.stderr)
    return []


def handle_command(text, cfg, today):
    """-> (답장 or None, /status 요청 여부). cfg["watches"]를 직접 수정한다."""
    cmd, _, arg = text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    if cmd in ("/start", "/help"):
        return HELP, False
    if cmd in ("/watch", "/add"):
        if not arg:
            return "날짜를 같이 보내주세요.\n\n" + HELP, False
        try:
            p = parse_watch_text(arg, today)
        except ValueError as e:
            return f"이해하지 못했어요: {e}\n\n{HELP}", False
        wid = max([e["id"] for e in cfg["watches"]] + [0]) + 1
        cfg["watches"].append({"id": wid, "text": arg, "ref": today.isoformat()})
        return f"감시 #{wid} 추가했어요: {describe(p)}", False
    if cmd in ("/list", "/ls"):
        ok, bad = compile_watches(cfg, today)
        lines = [f"#{w['id']} {describe(w['p'])}" for w in ok] + [f"#{e['id']} (오류) {e.get('text')}: {m}" for e, m in bad]
        return ("감시 목록\n" + "\n".join(lines)) if lines else "감시 중인 조건이 없어요. /watch 로 추가하세요.", False
    if cmd in ("/remove", "/unwatch", "/del"):
        ids = {int(x) for x in re.findall(r"\d+", arg)}
        gone = [e for e in cfg["watches"] if e["id"] in ids]
        cfg["watches"] = [e for e in cfg["watches"] if e["id"] not in ids]
        return (f"삭제했어요: {', '.join('#' + str(e['id']) for e in gone)}" if gone
                else "삭제할 번호를 찾지 못했어요. /list 로 번호를 확인하세요."), False
    if cmd == "/clear":
        n, cfg["watches"] = len(cfg["watches"]), []
        return f"감시 {n}건을 모두 삭제했어요.", False
    if cmd == "/status":
        return None, True
    return "알 수 없는 명령이에요.\n\n" + HELP, False


# ---------------------------------------------------------------- 메시지
def lottery_deadline(d):
    """이용일 -> (추첨 접수 마감 시각, 당첨확정 기한 날짜). 2024년 안내 규정 기준."""
    prev_last = d.replace(day=1) - timedelta(days=1)
    dl = datetime(prev_last.year, prev_last.month, 5 if d.day <= 15 else 20, 17, 0)
    return dl, dl.date() + timedelta(days=2)


def fmt_dt(dt):
    return f"{fmt_day(dt.date() if isinstance(dt, datetime) else dt)} {dt:%H:%M}" if isinstance(dt, datetime) else fmt_day(dt)


def split_key(k):
    d_s, room = k.split("|", 1)
    return date.fromisoformat(d_s), room


def build_alert(keys, prev):
    lines = ["[예약 가능] 전북교육청교직원수련원", ""]
    by_date = {}
    for k in sorted(keys):
        d, room = split_key(k)
        by_date.setdefault(d, []).append((room, prev.get(k)))
    for d, items in by_date.items():
        lines.append(fmt_day(d))
        lines += [f" - {r} ({b} → 예약가능)" if b else f" - {r} (신규 오픈)" for r, b in items]
        lines.append("")
    lines.append(f"바로 예약: {URL}")
    return "\n".join(lines)


def build_lottery_alert(keys):
    groups = {}
    for d in sorted({split_key(k)[0] for k in keys}):
        groups.setdefault(lottery_deadline(d), []).append(d)
    lines = ["[추첨 접수 중] 전북교육청교직원수련원", ""]
    for (dl, confirm), days in groups.items():
        lines.append(", ".join(fmt_day(d) for d in days))
        lines.append(f" 접수 마감 {fmt_dt(dl)} → 당첨발표 같은 날 20:00 → 당첨확정 {fmt_day(confirm)} 20:00까지")
    lines += ["", "※ 2024년 안내 기준이에요. 정확한 일정은 사이트에서 확인하세요.", f"신청: {URL}"]
    return "\n".join(lines)


def build_summary(new_watches, cells):
    lines = ["[감시 조건 반영] 상태가 바뀌면 알려드릴게요."]
    for w in new_watches:
        lines += ["", f"#{w['id']} {describe(w['p'])}"]
        by_date = {}
        for k, st in cells.items():
            if key_matches(w, k):
                d, room = split_key(k)
                by_date.setdefault(d, {}).setdefault(st, []).append(room)
        if not by_date:
            lines.append(" 아직 예약현황 표에 없는 날짜예요. (표에는 30일 전부터 나와요)")
        for d in sorted(by_date):
            st = by_date[d]
            avail = sorted(st.get(AVAILABLE, []))
            others = " / ".join(f"{s} {len(r)}" for s, r in st.items() if s != AVAILABLE)
            lines.append(f" {fmt_day(d)}: 예약가능 {len(avail)}개" + (f" / {others}" if others else ""))
            lines += [f"   - {r}" for r in avail[:6]]
            if len(avail) > 6:
                lines.append(f"   - 외 {len(avail) - 6}개")
            if LOTTERY in st:
                lines.append(f"   ※ 추첨 접수 중 (규정상 {fmt_dt(lottery_deadline(d)[0])} 마감)")
    return "\n".join(lines)


def build_status(now, cells, err, watches, watched):
    lines = [f"[상태] {now:%m/%d %H:%M} 확인"]
    if err:
        lines.append(f"사이트를 읽지 못했어요: {err}")
    else:
        a = sum(v == AVAILABLE for v in watched.values())
        lo = sum(v == LOTTERY for v in watched.values())
        lines.append(f"예약현황 {len(cells)}칸 읽음 / 감시 {len(watches)}건, 대상 {len(watched)}칸 중 예약가능 {a}, 추첨제 {lo}")
    lines += [f"#{w['id']} {describe(w['p'])}" for w in watches] or ["감시 중인 조건이 없어요. /watch 로 추가하세요."]
    return "\n".join(lines)


# ---------------------------------------------------------------- 메인 로직
def run(args):
    now = datetime.now(KST)
    today = date.fromisoformat(args.today) if args.today else now.date()
    cfg = load_config(args.config)
    state = load_json(args.state, {})
    threshold = int(cfg["fail_alert_after"])
    out = []  # 보낼 메시지 (순서대로)

    cfg_before = json.dumps(cfg, sort_keys=True)
    normalize(cfg, today)
    for w in prune_expired(cfg, today):
        out.append(f"지난 날짜라 감시를 종료했어요: #{w['id']} {describe(w['p'])}")

    cells, err = None, None
    try:
        cells = get_cells(args, today)
    except Exception as e:  # noqa: BLE001
        err = str(e)

    # 텔레그램 명령
    offset, want_status = int(state.get("offset", 0)), False
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    for u in read_updates(args, offset):
        offset = max(offset, int(u["update_id"]) + 1)
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/") or str((msg.get("chat") or {}).get("id", "")) != allowed:
            continue
        reply, st = handle_command(text, cfg, today)
        want_status = want_status or st
        if reply:
            out.append(reply)

    watches, bad = compile_watches(cfg, today)
    bad_sigs = sorted(hashlib.sha1(f"{e.get('id')}|{e.get('text')}|{m}".encode()).hexdigest()[:10] for e, m in bad)
    for (e, m), s in zip(bad, bad_sigs):
        if s not in state.get("bad", []):
            out.append(f"config.json의 감시 #{e.get('id')} 를 읽지 못했어요: {e.get('text')}\n원인: {m}")

    new_state = {"offset": offset, "sigs": state.get("sigs", {}), "cells": state.get("cells", {}),
                 "fail_count": int(state.get("fail_count", 0)), "bad": bad_sigs}

    if err:
        prev_fails = new_state["fail_count"]
        fails = min(prev_fails + 1, threshold)
        print(f"[실패 {fails}/{threshold}] {err}", file=sys.stderr)
        if prev_fails < threshold <= fails:
            out.append(f"[감시 오류] 예약현황을 연속 {threshold}번 읽지 못했어요.\n원인: {err}")
        new_state["fail_count"] = fails
        if want_status:
            out.append(build_status(now, None, err, watches, {}))
    else:
        matched = {}
        for k in cells:
            ids = {w["id"] for w in watches if key_matches(w, k)}
            if ids:
                matched[k] = ids
        watched = {k: cells[k] for k in matched}
        prev, prev_sigs = state.get("cells", {}), state.get("sigs", {})
        new_watches = [w for w in watches if prev_sigs.get(str(w["id"])) != w["sig"]]
        new_ids = {w["id"] for w in new_watches}
        stable = [k for k, ids in matched.items() if ids - new_ids]  # 이미 감시 중이던 조건에 걸리는 칸

        if new_watches:
            out.append(build_summary(new_watches, cells))
        opened = [k for k in stable if watched[k] == AVAILABLE and prev.get(k) != AVAILABLE]
        lottery = [k for k in stable if watched[k] == LOTTERY and prev.get(k) != LOTTERY]
        if opened:
            out.append(build_alert(opened, prev))
        if lottery:
            out.append(build_lottery_alert(lottery))
        if new_state["fail_count"] >= threshold:
            out.append("[감시 복구] 예약현황을 다시 읽을 수 있어요.")
        if want_status:
            out.append(build_status(now, cells, None, watches, watched))
        if not out:
            print(f"변화 없음 (감시 {len(watched)}칸, 예약가능 {sum(v == AVAILABLE for v in watched.values())}칸)")
        new_state.update({"sigs": {str(w["id"]): w["sig"] for w in watches}, "cells": watched, "fail_count": 0})

    for m in out:  # 전송 실패 시 예외 -> 상태 저장 안 함 -> 다음 실행에서 다시 시도
        send_telegram(m, args.dry_run)
    if not args.dry_run or args.save:
        if json.dumps(cfg, sort_keys=True) != cfg_before:
            write_json(args.config, cfg)
        write_json(args.state, new_state)
    return 0


def run_test(args):
    today = datetime.now(KST).date()
    try:
        n = len(get_cells(args, today))
        msg, ok = f"테스트 성공. 텔레그램 연결 OK, 예약현황 {n}칸을 읽었어요.", True
    except Exception as e:  # noqa: BLE001
        msg, ok = f"텔레그램 연결은 OK인데 사이트를 읽지 못했어요.\n원인: {e}", False
    send_telegram(msg, args.dry_run)
    print(msg)
    return 0 if ok else 1


def _in_open_window(now):
    """매월 8일·23일 09:55~10:30 (잔여·취소 객실 선착순 오픈 직후)"""
    return now.day in (8, 23) and (9, 55) <= (now.hour, now.minute) < (10, 30)


def _wait(args, seconds):
    """seconds 동안 대기하되, 텔레그램 새 메시지가 오면 바로 깨어난다 (롱폴링으로 읽기만 함, 확정은 run()이)."""
    end = time.monotonic() + seconds
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        time.sleep(seconds)
        return
    while (left := end - time.monotonic()) > 0:
        try:
            offset = load_json(args.state, {}).get("offset")
            params = {"timeout": int(min(25, left)), "allowed_updates": '["message"]'}
            if offset is not None:
                params["offset"] = offset
            if tg_call("getUpdates", params):
                time.sleep(min(2, max(0, end - time.monotonic())))  # 처리 못 하는 메시지로 인한 공회전 방지
                return
        except Exception:  # noqa: BLE001
            time.sleep(min(5, max(0, end - time.monotonic())))


def loop(args):
    lo, hi = (int(x) for x in args.quiet.split("-"))
    print(f"감시 시작 (간격 {args.interval}초, 쉬는 시간 {lo}~{hi}시). 종료는 Ctrl+C", flush=True)
    errors = 0
    while True:
        now = datetime.now(KST)
        wait = args.interval
        if not (lo <= now.hour < hi):
            try:
                run(args)
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"[오류] {e}", file=sys.stderr, flush=True)
        if errors:
            wait = min(args.interval * 2 ** errors, 900)  # 연속 실패 시 지수적으로 물러나기
        elif _in_open_window(now):
            wait = min(wait, 30)
        _wait(args, max(30, wait))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--html-file", help="사이트 대신 저장한 HTML 파일 사용 (테스트용)")
    ap.add_argument("--updates-file", help="텔레그램 명령 대신 JSON 파일 사용 (테스트용)")
    ap.add_argument("--today", help="오늘 날짜 고정 YYYY-MM-DD (테스트용)")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송/상태 저장 없이 화면에만 출력")
    ap.add_argument("--save", action="store_true", help="--dry-run 이어도 상태를 저장 (테스트용)")
    ap.add_argument("--test", action="store_true", help="텔레그램 + 사이트 접속 테스트")
    ap.add_argument("--loop", action="store_true", help="계속 실행 (내 PC용)")
    ap.add_argument("--interval", type=int, default=300, help="--loop 확인 간격(초)")
    ap.add_argument("--quiet", default="1-5", help="--loop 쉬는 시간대 (KST, 예: 1-5)")
    args = ap.parse_args()
    sys.exit(run_test(args) if args.test else (loop(args) if args.loop else run(args)))


if __name__ == "__main__":
    main()
