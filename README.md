# 유니드 울산공장 온열질환 모니터링

## 기능

- 기상청 체감온도 API 조회
- 한국시간 08:00~17:00 매시간 자동 실행
- 체감온도 기준 위험단계 판정
- 단계 변경 시 Microsoft Teams 알림
- GitHub Pages 대시보드 갱신

## 기준

- 정상: 31℃ 미만
- 주의: 31℃ 이상
- 경계: 33℃ 이상
- 위험: 35℃ 이상
- 매우위험: 38℃ 이상

## GitHub Secrets

- `KMA_AUTH_KEY`
- `TEAMS_WEBHOOK_URL`
