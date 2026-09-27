# g2b.go.kr (나라장터) 크롤링 패턴

> 마지막 검증: 2026-03-31  
> 수집 결과: 교량 입찰공고 203/203건 (100%)

---

## 사이트 특성

- **프레임워크**: WebSquare (엔터프라이즈 SPA) — URL이 변하지 않는 SPA 내비게이션
- **분류**: SPA 세션 보호 사이트 (전략 E)
- **인증**: 로그인 불필요 (공개 데이터)
- **안티봇**: API 직접 호출 시 403 반환 (세션 쿠키 + WebSquare 커스텀 헤더 필수)
- **페이지네이션**: startIndex/endIndex 기반 scroll type

---

## 주요 장벽

### 1. 팝업이 클릭 이벤트 차단
메인 페이지 로드 시 w2window/layer 팝업이 뜨며 pointer events를 가로챔.

**해결책:**
```python
page.keyboard.press("Escape")
page.evaluate("""() => {
    document.querySelectorAll('[role="dialog"],[class*="popup"],[class*="w2window"],[class*="layer"]')
        .forEach(m => { m.style.display = 'none'; m.style.visibility = 'hidden'; });
}""")
```

### 2. WebSquare 입력 필드 — DOM 주입 불가
`element.value = "교량"` 방식은 gnb 전역 검색창에 입력되거나 WebSquare 내부 상태에 반영 안 됨.

**해결책:** `page.fill(selector, value)` 사용 (Playwright 내장 fill이 WebSquare 이벤트 트리거함)

### 3. 추가 페이지 API 호출 시 403
WebSquare 커스텀 헤더 없이 API 호출 시 403 반환.

**해결책:** 첫 요청에서 헤더를 캡처하여 재사용

---

## 필수 셀렉터 IDs (2026-03-31 기준)

| 용도 | 셀렉터 ID |
|------|-----------|
| 공고명 입력 | `#mf_wfm_container_tacBidPbancLst_contents_tab2_body_bidPbancNm` |
| 검색 버튼 | `#mf_wfm_container_tacBidPbancLst_contents_tab2_body_btnS0004` |
| 페이지크기 select | `#mf_wfm_container_tacBidPbancLst_contents_tab2_body_sbxRecordCountPerPage1` |

---

## API 엔드포인트

```
POST https://www.g2b.go.kr/pn/pnp/pnpe/BidPbac/selectBidPbacScrollTypeList.do
Content-Type: application/json;charset=UTF-8
```

### 필수 WebSquare 커스텀 헤더 (3개)
```
submissionid: <첫 요청에서 캡처>
menu-info: <첫 요청에서 캡처>
target-id: <첫 요청에서 캡처>
```

### 페이지네이션 파라미터
```json
{
  "dlBidPbancLstM": {
    "startIndex": 1,
    "endIndex": 100
  }
}
```

---

## 수집 코드 패턴 (핵심)

```python
first_request_body = [None]
first_request_headers = [{}]

def on_request(request):
    """첫 번째 selectBidPbac 요청 바디 + 헤더 캡처"""
    if "selectBidPbac" in request.url and first_request_body[0] is None:
        body = request.post_data
        if body:
            first_request_body[0] = body
            first_request_headers[0] = dict(request.headers)

def on_response(response):
    """XHR 응답 리스너 — 첫 100건 캡처"""
    if "selectBidPbac" in response.url and response.status == 200:
        data = response.json()
        items = data.get("result", [])
        # ... 수집 처리

page.on("request", on_request)
page.on("response", on_response)

# 추가 페이지: 브라우저 내 fetch()로 WebSquare 세션 재사용
hdrs = first_request_headers[0]
ws_headers = {
    "Content-Type": hdrs.get("content-type", "application/json;charset=UTF-8"),
    "accept": hdrs.get("accept", "application/json"),
    "submissionid": hdrs.get("submissionid", ""),   # 필수
    "menu-info": hdrs.get("menu-info", ""),          # 필수
    "target-id": hdrs.get("target-id", ""),          # 필수
    "usr-id": hdrs.get("usr-id", "null"),
    "referer": "https://www.g2b.go.kr/",
}

base_body = json.loads(first_request_body[0])
base_body["dlBidPbancLstM"]["startIndex"] = 101
base_body["dlBidPbancLstM"]["endIndex"] = 200
payload = json.dumps(base_body, ensure_ascii=False)

result = page.evaluate(f"""
    async () => {{
        const resp = await fetch('{api_url}', {{
            method: 'POST',
            headers: {json.dumps(ws_headers)},
            body: {json.dumps(payload)}
        }});
        if (!resp.ok) return {{error: resp.status}};
        return await resp.json();
    }}
""")
```

---

## 네비게이션 순서

```
1. https://www.g2b.go.kr/ 로드 (networkidle)
2. 팝업 제거 (Escape + display:none)
3. 입찰 GNB 메뉴 클릭 → "입찰공고목록" 클릭
   ※ page.locator() 대신 page.evaluate()로 JS 클릭 권장 (팝업 잔재 우회)
4. 페이지크기 select → 100 설정
5. 공고명 input → page.fill() 로 키워드 입력
6. 검색 버튼 클릭 → 10초 대기 (XHR 캡처)
7. 추가 페이지: 브라우저 내 fetch() 반복
```

---

## 응답 JSON 필드 매핑

| 수집 항목 | API 필드명 |
|-----------|-----------|
| 공고번호 | `bidPbancUntyNoOrd` |
| 업무구분 | `prcmBsneSeCdNm` |
| 공고명 | `bidPbancNm` |
| 발주기관 | `oderInstUntyGrpNm` |
| 수요기관 | `dmstNm` |
| 게시/마감일시 | `pbancPstgDt` (HTML 포함, 파싱 필요) |
| 추정가격 | `prspPrce` |
| 배정예산 | `alotBgtAmt` |
| 낙찰방법 | `scsbdMthdNm` |
| 공고상태 | `pbancSttsNm` |

응답 구조: `{ "result": [...], "totCnt": 203 }`

---

## 참고 스크립트

- 재사용 스크립트(키워드 인자): `python scripts/g2b_bid_crawl.py 교량 --days 30` — 출력은 `output/g2b.go.kr/<키워드>_입찰공고_<ts>/`, 실행 후 profile.json notes 자동 누적
- `output/g2b.go.kr/교량입찰공고_20260331_172006/crawl_script.py`
