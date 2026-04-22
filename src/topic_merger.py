"""토픽 기반 에피소드 병합."""
from __future__ import annotations

import logging
from difflib import SequenceMatcher

from src.summarizer import MeetingAnalysis, MeetingProperties, MeetingContent

log = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.6


def merge_analyses_by_topic(analyses: list[MeetingAnalysis]) -> list[MeetingAnalysis]:
    """여러 에피소드의 분석 결과를 토픽 유사성 기반으로 병합.

    병합 기준:
    1. 동일 고객명 (properties.client)
    2. 토픽명 유사도 >= 0.6
    3. 동일 프로젝트명 (properties.project)
    """
    if len(analyses) <= 1:
        return analyses

    n = len(analyses)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge(analyses[i], analyses[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    result = []
    for indices in groups.values():
        if len(indices) == 1:
            result.append(analyses[indices[0]])
        else:
            merged = _merge_group([analyses[i] for i in indices])
            result.append(merged)
            log.info(
                "토픽 병합: %s → %s",
                [analyses[i].properties.title for i in indices],
                merged.properties.title,
            )

    return result


def _should_merge(a: MeetingAnalysis, b: MeetingAnalysis) -> bool:
    """두 에피소드를 병합해야 하는지 판단."""
    # 동일 고객명
    if a.properties.client and b.properties.client:
        if a.properties.client == b.properties.client:
            return True

    # 동일 프로젝트명
    if a.properties.project and b.properties.project:
        if a.properties.project == b.properties.project:
            return True

    # 토픽명 유사도
    for t_a in a.properties.topics:
        for t_b in b.properties.topics:
            if _topic_similarity(t_a, t_b) >= _SIMILARITY_THRESHOLD:
                return True

    return False


def _topic_similarity(a: str, b: str) -> float:
    """두 토픽명의 유사도 (0~1)."""
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    return SequenceMatcher(None, a, b).ratio()


def _merge_group(analyses: list[MeetingAnalysis]) -> MeetingAnalysis:
    """여러 에피소드를 하나로 병합."""
    base = analyses[0]

    # 토픽별 상세 내용 통합
    all_topics = []
    for a in analyses:
        all_topics.extend(a.content.topics)

    # properties 통합 (중복 제거, 순서 유지)
    all_people = []
    all_tags = []
    all_entities = []
    all_topic_names = []
    for a in analyses:
        all_people.extend(a.properties.related_people)
        all_tags.extend(a.properties.tags)
        all_entities.extend(a.properties.entities)
        all_topic_names.extend(a.properties.topics)

    merged_props = MeetingProperties(
        title=base.properties.title,
        meeting_type=base.properties.meeting_type,
        project=base.properties.project,
        client=base.properties.client,
        related_people=list(dict.fromkeys(all_people)),
        tags=list(dict.fromkeys(all_tags)),
        priority=base.properties.priority,
        status=base.properties.status,
        summary=base.properties.summary,
        next_actions="\n".join(
            a.properties.next_actions for a in analyses if a.properties.next_actions
        ),
        participants=", ".join(
            dict.fromkeys(
                p.strip()
                for a in analyses
                for p in a.properties.participants.split(",")
                if p.strip()
            )
        ),
        date=base.properties.date,
        knowledge_types=list(
            dict.fromkeys(kt for a in analyses for kt in a.properties.knowledge_types)
        ),
        entities=list(dict.fromkeys(all_entities)),
        topics=list(dict.fromkeys(all_topic_names)),
    )

    # content 통합
    all_decisions = []
    all_risks = []
    all_actions = []
    for a in analyses:
        all_decisions.extend(a.content.decisions)
        all_risks.extend(a.content.risks)
        all_actions.extend(a.content.action_items)

    merged_content = MeetingContent(
        executive_summary=base.content.executive_summary,
        topics=all_topics,
        decisions=list(dict.fromkeys(all_decisions)),
        risks=list(dict.fromkeys(all_risks)),
        action_items=all_actions,
        key_persons=base.content.key_persons,
    )

    return MeetingAnalysis(properties=merged_props, content=merged_content)
