"""scripts/g2b_bid_crawl.py — 나라장터(g2b.go.kr) 입찰공고 키워드 수집

fingerprints/g2b_go_kr/profile.json 레시피(사다리 3단, playwright_spa_intercept)를 그대로 쓴다.
  1) 메인 → 팝업 제거 → 입찰 > 입찰공고목록
  2) 공고명에 키워드 page.fill() → 검색 → 첫 selectBidPbac XHR 요청/응답 캡처
  3) 추가 페이지는 캡처한 바디·WebSquare 헤더로 브라우저 내 fetch() (세션 재사용)
  4) 공고번호 기준 dedup → raw_data.json / crawl_result.xlsx → profile.json 갱신

사용:
    python scripts/g2b_bid_crawl.py 교량
    python scripts/g2b_bid_crawl.py 함정 경비함 예인선 --days 30
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright  # noqa: E402

from domain_profile import DomainProfile  # noqa: E402
from export_excel import export_to_excel  # noqa: E402
from utils import (RateLimiter, check_robots, detect_pii, detect_softblock,  # noqa: E402
                   setup_logger, validate_values)

DOMAIN = "g2b.go.kr"
BASE_URL = "https://www.g2b.go.kr/"
API_URL = "https://www.g2b.go.kr/pn/pnp/pnpe/BidPbac/selectBidPbacScrollTypeList.do"
PREFIX = "#mf_wfm_container_tacBidPbancLst_contents_tab2_body_"
SEL_KEYWORD = PREFIX + "bidPbancNm"
SEL_PAGESIZE = PREFIX + "sbxRecordCountPerPage1"
SEL_SEARCH = PREFIX + "btnS0004"
PAGE_SIZE = 100
MAX_PAGES = 30  # 키워드당 상한 3,000건 — 부담 축

logger = setup_logger("g2b_bid_crawl")

DATE_RE = re.compile(r"(\d{4})[/.-](\d{2})[/.-](\d{2})(?:\s+(\d{2}):(\d{2}))?")


def parse_post_date(raw) -> str:
    """pbancPstgDt 는 '게시일시<br>(마감일시)' 형태의 HTML — 첫 일시만 게시일로 쓴다."""
    if not raw:
        return ""
    m = DATE_RE.search(re.sub(r"<[^>]+>", " ", str(raw)))
    if not m:
        return str(raw).strip()
    y, mo, d, hh, mm = m.groups()
    return f"{y}-{mo}-{d}" + (f" {hh}:{mm}" if hh else "")


def parse_price(raw):
    if raw in (None, ""):
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None


def to_row(item: dict) -> dict:
    return {
        "공고번호": item.get("bidPbancUntyNoOrd") or "",
        "공고명": (item.get("bidPbancNm") or "").strip(),
        "발주기관": item.get("oderInstUntyGrpNm") or "",
        "추정가격": parse_price(item.get("prspPrce")),
        "게시일": parse_post_date(item.get("pbancPstgDt")),
        "공고상태": item.get("pbancSttsNm") or "",
        "업무구분": item.get("prcmBsneSeCdNm") or "",
        "수요기관": item.get("dmstNm") or "",
    }


def close_popups(page):
    page.keyboard.press("Escape")
    page.evaluate("""() => {
        document.querySelectorAll('[role="dialog"],[class*="popup"],[class*="w2window"],[class*="layer"]')
            .forEach(m => { m.style.display = 'none'; m.style.visibility = 'hidden'; });
    }""")


def open_bid_list(page):
    page.goto(BASE_URL, wait_until="networkidle", timeout=90_000)
    page.wait_for_timeout(2000)
    close_popups(page)
    # 팝업 잔재가 pointer events 를 가로채므로 JS 클릭 (recipe.md 권장)
    page.evaluate("""() => {
        const a = [...document.querySelectorAll('a[id*="gnbMenu"]')]
            .find(e => e.textContent.trim() === '\\uC785\\uCC30');
        if (a) a.click();
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        const a = [...document.querySelectorAll('a')]
            .find(e => e.textContent.trim() === '\\uC785\\uCC30\\uACF5\\uACE0\\uBAA9\\uB85D');
        if (a) a.click();
    }""")
    page.wait_for_selector(SEL_SEARCH, timeout=60_000)
    page.wait_for_timeout(1500)
    close_popups(page)
    try:
        page.select_option(SEL_PAGESIZE, str(PAGE_SIZE))
    except Exception as e:  # 기본 페이지 크기로도 fetch 페이지네이션은 동작
        logger.warning(f"페이지크기 설정 실패(무시): {e}")


def browser_fetch(page, headers: dict, body: dict):
    return page.evaluate(
        """async ([url, headers, payload]) => {
            const r = await fetch(url, {method: 'POST', headers, body: payload});
            if (!r.ok) return {error: r.status};
            return await r.json();
        }""",
        [API_URL, headers, json.dumps(body, ensure_ascii=False)],
    )


def crawl_keyword(page, keyword: str, limiter: RateLimiter) -> list[dict]:
    captured = {"body": None, "headers": {}, "first": None}

    def on_request(req):
        if "selectBidPbac" in req.url and req.post_data and captured["body"] is None:
            captured["body"] = req.post_data
            captured["headers"] = dict(req.headers)

    def on_response(resp):
        if "selectBidPbac" in resp.url and captured["first"] is None:
            try:
                captured["first"] = {"status": resp.status, "json": resp.json()}
            except Exception:
                captured["first"] = {"status": resp.status, "text": resp.text()[:5000]}

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        page.fill(SEL_KEYWORD, keyword)
        page.click(SEL_SEARCH)
        for _ in range(30):
            if captured["first"]:
                break
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    first = captured["first"]
    if not first or "json" not in first:
        # Step 5.0 — JSON 이 아니면 소프트블록 여부부터 본다
        text = (first or {}).get("text", page.content())
        verdict = detect_softblock(text, status=(first or {}).get("status", 0))
        raise RuntimeError(f"[{keyword}] 첫 응답 캡처 실패 — {verdict['verdict']} {verdict['signals']}")

    data = first["json"]
    items = list(data.get("result") or [])
    total = int(data.get("totCnt") or len(items))
    logger.info(f"[{keyword}] totCnt={total}, 첫 응답 {len(items)}건")

    hdrs = captured["headers"]
    ws_headers = {
        "Content-Type": hdrs.get("content-type", "application/json;charset=UTF-8"),
        "accept": hdrs.get("accept", "application/json"),
        "submissionid": hdrs.get("submissionid", ""),
        "menu-info": hdrs.get("menu-info", ""),
        "target-id": hdrs.get("target-id", ""),
        "usr-id": hdrs.get("usr-id", "null"),
    }
    base_body = json.loads(captured["body"])
    page_size = len(items) or PAGE_SIZE
    start = len(items) + 1
    errors = 0
    pages = 1
    while start <= total and pages < MAX_PAGES:
        limiter.wait()
        base_body["dlBidPbancLstM"]["startIndex"] = start
        base_body["dlBidPbancLstM"]["endIndex"] = start + page_size - 1
        try:
            res = browser_fetch(page, ws_headers, base_body)
            if not isinstance(res, dict) or "error" in res:
                raise RuntimeError(f"HTTP {res.get('error') if isinstance(res, dict) else res}")
            chunk = res.get("result") or []
            if not chunk:
                break
            items.extend(chunk)
            limiter.reset_errors()
            errors = 0
            logger.info(f"[{keyword}] {start}~ : +{len(chunk)} (누적 {len(items)}/{total})")
        except Exception as e:
            errors += 1
            logger.warning(f"[{keyword}] startIndex={start} 실패: {e}")
            if errors >= 5:
                logger.error(f"[{keyword}] 연속 5회 실패 — 중단")
                break
            continue
        start += page_size
        pages += 1
    return items


def main():
    ap = argparse.ArgumentParser(description="나라장터 입찰공고 키워드 수집")
    ap.add_argument("keywords", nargs="+", help="공고명 검색 키워드 (여러 개면 OR, 공고번호 dedup)")
    ap.add_argument("--days", type=int, default=30, help="게시일 기준 최근 N일만 남김 (0=필터 없음)")
    ap.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    args = ap.parse_args()

    robots = check_robots(BASE_URL)
    if robots.get("error"):
        logger.warning(f"robots.txt 확인 못 함: {robots['error']}")
    elif not robots.get("allowed"):
        sys.exit("robots.txt 가 대상 경로를 차단합니다 — 진행 여부를 먼저 결정하세요")

    topic = "_".join(args.keywords) + "_입찰공고"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "output", DOMAIN, f"{topic}_{ts}")
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy(os.path.abspath(__file__), os.path.join(out_dir, "crawl_script.py"))

    limiter = RateLimiter(delay=0.3, min_delay=0.2, max_requests=MAX_PAGES * len(args.keywords))
    t0 = time.time()
    raw = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(locale="ko-KR")
        open_bid_list(page)
        for kw in args.keywords:
            try:
                raw.extend(crawl_keyword(page, kw, limiter))
            except Exception as e:
                logger.error(str(e))
                continue
        browser.close()

    seen, rows = set(), []
    for it in raw:
        row = to_row(it)
        key = row["공고번호"] or row["공고명"]
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    if args.days:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()
        rows = [r for r in rows if not r["게시일"] or r["게시일"][:10] >= cutoff]
    rows.sort(key=lambda r: r["게시일"], reverse=True)

    if not rows:
        sys.exit("수집 0건 — 기존 산출물을 덮지 않고 중단합니다. 셀렉터/흐름 변경 여부를 확인하세요")

    with open(os.path.join(out_dir, "raw_data.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    issues = validate_values(rows, {
        "공고명": {"type": "str", "required": True, "max_empty_ratio": 0.1},
        "발주기관": {"type": "str", "required": True, "max_empty_ratio": 0.1},
        "추정가격": {"type": "int", "min": 0, "max": 10_000_000_000_000, "max_empty_ratio": 0.5},
        "게시일": {"type": "str", "required": True, "max_empty_ratio": 0.1},
        "공고상태": {"type": "str", "required": True, "allow_uniform": True},
    })
    for w in issues + detect_pii(rows):
        logger.warning(w)

    xlsx = export_to_excel(rows, os.path.join(out_dir, "crawl_result.xlsx"), sheet_name="입찰공고")

    # Step 5-A — 프로필 갱신 (notes 누적, 사다리 A 그대로)
    mgr = DomainProfile(base_dir=os.path.join(root, "fingerprints"))
    prof = mgr.load(DOMAIN) or {"domain": DOMAIN}
    notes = prof.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    notes.append(f"{date.today()} '{'/'.join(args.keywords)}' 최근 {args.days}일 {len(rows)}건 수집 "
                 f"— scripts/g2b_bid_crawl.py (키워드 인자 재사용 스크립트)")
    prof.update({"notes": notes, "last_used": str(date.today())})
    mgr.save(DOMAIN, prof)

    fill = {k: sum(1 for r in rows if r[k] not in (None, "")) / len(rows) for k in rows[0]}
    print(f"\n엑셀: {xlsx}\n건수: {len(rows)}  소요: {time.time() - t0:.1f}s")
    print("채움률: " + ", ".join(f"{k} {v:.0%}" for k, v in fill.items()))


if __name__ == "__main__":
    main()
