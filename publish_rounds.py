#!/usr/bin/env python3
"""
로또전문가 매주 당첨번호 파일 갱신.

GitHub 저장소의 rounds.json을 읽어 마지막 회차 다음부터 동행복권에서 새 회차를 받아 붙이고,
다시 올린다. 앱은 GitHub Pages 주소로 이 파일을 읽기만 한다. DB는 쓰지 않는다.

필요한 것
  - Python 3.8 이상 (추가 설치 없음)
  - 환경변수
      GITHUB_TOKEN   이 저장소에만 Contents 쓰기 권한이 있는 토큰 (절대 코드에 적지 않는다)
      GITHUB_REPO    예: yourname/lotteryexpert-data
      GITHUB_BRANCH  기본 main
      ROUNDS_PATH    기본 rounds.json

실행 예 (서버 시간이 한국 시간일 때 crontab)
  30 21 * * 6  /usr/bin/python3 /path/publish_rounds.py >> /var/log/lotteryexpert-rounds.log 2>&1
  0  9  * * 0  /usr/bin/python3 /path/publish_rounds.py >> /var/log/lotteryexpert-rounds.log 2>&1
  토요일 21시 30분에 한 번, 발표가 늦어질 때를 대비해 일요일 9시에 한 번 더 돌린다. 새 회차가 없으면 아무것도 올리지 않는다.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DHLOTTERY_URL = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd={}"
DHLOTTERY_HEADERS = {
    "Referer": "https://www.dhlottery.co.kr/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.3 Safari/605.1.15",
}
KST = timezone(timedelta(hours=9))
# 한 번 실행에 붙일 최대 회차 수 — 오래 멈췄다 다시 돌려도 동행복권에 부담을 주지 않게
MAX_NEW_ROUNDS = 20


def required_env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"환경변수 {name} 이(가) 필요해요")
    return value


def request_json(url: str, headers: dict, data: bytes = None, method: str = "GET"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def is_valid(round_: dict) -> bool:
    numbers = round_["numbers"]
    return (
        len(numbers) == 6
        and len(set(numbers)) == 6
        and all(1 <= n <= 45 for n in numbers)
        and 1 <= round_["bonus"] <= 45
        and round_["bonus"] not in numbers
        and len(round_["drawDate"]) == 8
        and round_["drawDate"].isdigit()
    )


def fetch_round(number: int):
    """추첨이 끝난 회차면 회차 정보를, 아직이면 None을 돌려준다. 네트워크 오류는 몇 번 다시 시도한다."""
    last_error = None
    for attempt in range(4):
        try:
            body = request_json(DHLOTTERY_URL.format(number), DHLOTTERY_HEADERS)
            items = ((body or {}).get("data") or {}).get("list") or []
            if not items:
                return None
            item = items[0]
            if int(item.get("ltEpsd", 0)) != number:
                return None
            return {
                "round": number,
                "drawDate": str(item["ltRflYmd"]),
                "numbers": sorted(int(item[f"tm{i}WnNo"]) for i in range(1, 7)),
                "bonus": int(item["bnsWnNo"]),
            }
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{number}회 조회 실패: {last_error}")


def main() -> int:
    token = required_env("GITHUB_TOKEN")
    repo = required_env("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    path = os.environ.get("ROUNDS_PATH", "rounds.json")

    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    github_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lotteryexpert-rounds-publisher",
    }

    current = request_json(f"{api}?ref={branch}", github_headers)
    stored = json.loads(base64.b64decode(current["content"]).decode("utf-8"))
    rounds = stored["rounds"] if isinstance(stored, dict) else stored
    latest = max(r["round"] for r in rounds)

    added = []
    number = latest + 1
    while len(added) < MAX_NEW_ROUNDS:
        round_ = fetch_round(number)
        if round_ is None:
            break
        if not is_valid(round_):
            raise RuntimeError(f"{number}회 데이터가 올바르지 않아요: {round_}")
        added.append(round_)
        number += 1
        time.sleep(0.5)

    now = datetime.now(KST).isoformat(timespec="seconds")
    if not added:
        print(f"[{now}] 새 회차 없음 (마지막 {latest}회)")
        return 0

    payload = {"updatedAt": now, "rounds": rounds + added}
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = json.dumps(
        {
            "message": f"{added[0]['round']}~{added[-1]['round']}회 당첨번호 추가",
            "content": base64.b64encode(content).decode("ascii"),
            "sha": current["sha"],
            "branch": branch,
        }
    ).encode("utf-8")
    request_json(api, {**github_headers, "Content-Type": "application/json"}, data=body, method="PUT")
    print(f"[{now}] {added[0]['round']}~{added[-1]['round']}회 추가 완료")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # cron 로그에 원인을 남기고 실패 코드로 끝낸다
        print(f"[{datetime.now(KST).isoformat(timespec='seconds')}] 실패: {error}", file=sys.stderr)
        sys.exit(1)
