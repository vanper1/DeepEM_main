from __future__ import annotations

from deepem.protocol import CaseRecord, CaseStatus, Event, EventType, SignalRecord, StateSnapshot, new_id, utc_now
from deepem.state.repositories import CaseRepo, StateRepo


class EventProjector:
    def __init__(self, *, state_repo: StateRepo, case_repo: CaseRepo) -> None:
        self.state_repo = state_repo
        self.case_repo = case_repo

    def apply(self, event: Event) -> None:
        state = self.state_repo.get(event.task_id)
        if event.event_type == EventType.STRUCTURED_SIGNAL_DETECTED:
            self._apply_structured_signal(event, state)
            return
        if event.event_type == EventType.COLLECTION_COMPLETED:
            state.metadata["last_collection"] = event.payload
            self.state_repo.save(state)
            return
        if event.event_type == EventType.PROTOCOL_ANALYSIS_COMPLETED:
            self._apply_analysis(event)
            return
        if event.event_type == EventType.OPERATOR_FEEDBACK_RECEIVED:
            self._apply_feedback(event)
            return

    def _apply_structured_signal(self, event: Event, state: StateSnapshot) -> None:
        signal_id = str(event.payload["signal_id"])
        record = SignalRecord(
            signal_id=signal_id,
            fingerprint=str(event.payload["fingerprint"]),
            classification=str(event.payload.get("classification", "observed")),
            carries_information=bool(event.payload.get("carries_information", False)),
            suspected_device_type=event.payload.get("suspected_device_type"),
            evidence_refs=list(event.evidence_refs),
            metadata=dict(event.payload.get("features", {})),
            conversation_id=event.conversation_id,
        )
        state.active_signals[signal_id] = record
        self.state_repo.save(state)
        if record.classification in {"suspicious", "unknown"}:
            case = self.case_repo.get_by_signal(event.task_id, signal_id)
            if case is not None and case.conversation_id != event.conversation_id:
                case = None
            if case is None:
                case = CaseRecord(
                    id=new_id("case"),
                    task_id=event.task_id,
                    signal_id=signal_id,
                    status=CaseStatus.OPEN,
                    risk_level="high" if record.carries_information else "medium",
                    notes=["信号与当前场所基线存在偏离，需要进入调查流程。"],
                    evidence_refs=list(event.evidence_refs),
                    conversation_id=event.conversation_id,
                )
            self._extend_evidence_refs(case, event.evidence_refs)
            case.conversation_id = case.conversation_id or event.conversation_id
            self.case_repo.save(case)

    def _apply_analysis(self, event: Event) -> None:
        signal_id = str(event.payload["signal_id"])
        case = self.case_repo.get_by_signal(event.task_id, signal_id)
        if case is not None and case.conversation_id != event.conversation_id:
            case = None
        if case is None:
            case = CaseRecord(id=new_id("case"), task_id=event.task_id, signal_id=signal_id, status=CaseStatus.INVESTIGATING, risk_level=str(event.payload.get("risk_level", "medium")), conversation_id=event.conversation_id)
        verdict = str(event.payload.get("verdict", "abnormal"))
        case.status = CaseStatus.CLOSED if verdict == "likely_benign" else CaseStatus.INVESTIGATING
        case.risk_level = str(event.payload.get("risk_level", case.risk_level))
        case.hypothesis = event.payload.get("hypothesis")
        summary = event.payload.get("summary")
        if summary:
            case.notes.append(str(summary))
        self._extend_evidence_refs(case, event.evidence_refs)
        case.updated_at = utc_now()
        self.case_repo.save(case)

    def _apply_feedback(self, event: Event) -> None:
        signal_id = str(event.payload["signal_id"])
        verdict = str(event.payload["verdict"])
        note = str(event.payload.get("note", ""))
        case = self.case_repo.get_by_signal(event.task_id, signal_id)
        if case is not None and case.conversation_id != event.conversation_id:
            case = None
        if case is None:
            case = CaseRecord(id=new_id("case"), task_id=event.task_id, signal_id=signal_id, status=CaseStatus.OPEN, risk_level="medium", conversation_id=event.conversation_id)
        if verdict == "dismissed":
            case.status = CaseStatus.CLOSED
        elif verdict == "confirmed":
            case.status = CaseStatus.INVESTIGATING
        if note:
            case.notes.append(note)
        self._extend_evidence_refs(case, event.evidence_refs)
        case.updated_at = utc_now()
        self.case_repo.save(case)

    @staticmethod
    def _extend_evidence_refs(case: CaseRecord, evidence_refs) -> None:
        existing = {(item.kind, item.uri, item.label) for item in case.evidence_refs}
        for item in evidence_refs:
            key = (item.kind, item.uri, item.label)
            if key in existing:
                continue
            case.evidence_refs.append(item)
            existing.add(key)
