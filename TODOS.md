# TODOS — 003_voice_flow

## Deferred to v2
- [ ] **화자 프로필 학습** — SPEAKER_00/01을 실제 이름으로 자동 매핑 (음성 임베딩 기반)
- [ ] **미팅 문맥 연결** — 이전 미팅과의 연관성 자동 감지
- [ ] **다국어 STT** — 영어 등 다른 언어 지원

## Implementation Notes
- Notion "apple_db" page_id: 구현 시 Notion API search로 확인
- 미팅노트 템플릿: voice_flow 전용 (제목/참석자/요약/논의사항/액션아이템/전사본)
- 파일 안정화 타임아웃: 10분 초과 시 스킵 + 로그 (critical gap 대응)
