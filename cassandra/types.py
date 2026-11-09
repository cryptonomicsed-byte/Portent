"""Cassandra types (from core/types.ts), trimmed to what Portent uses.

Kept: ParadigmType, PredictionConfidence, ParadigmPrediction (timeline,
signals), CommitmentProof (was BlockchainProof), AccuracyMetrics,
EmergentPattern, LearningCycle, OracleMetrics.

Dropped with the code generator / executor: shadowBranch, TestResult,
CodeRefactorRequest, RefactorResult, SecurityScanResult, DeploymentPlan,
DeploymentStage, MonitoringPoint, AgentTask, and the 'deployed' status.

Times are unix seconds (int), not Date objects, so everything is JSON-safe
and hashes deterministically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ParadigmType(str, Enum):
    ARCHITECTURAL = "architectural"
    TECHNOLOGICAL = "technological"
    MARKET = "market"
    SECURITY = "security"
    PERFORMANCE = "performance"
    GOVERNANCE = "governance"


class PredictionConfidence(float, Enum):
    LOW = 0.3
    MEDIUM = 0.6
    HIGH = 0.85
    CRITICAL = 0.95


STATUSES = ("pending", "committed", "emerged", "validated", "refuted")


@dataclass
class Timeline:
    predicted_emergence: int
    earliest_signal: int
    critical_window: Tuple[int, int]

    def __post_init__(self) -> None:
        self.critical_window = (int(self.critical_window[0]), int(self.critical_window[1]))
        if self.critical_window[0] > self.critical_window[1]:
            raise ValueError("critical_window start after end")


@dataclass
class Signals:
    github_trends: List[str] = field(default_factory=list)
    arxiv_papers: List[str] = field(default_factory=list)
    news_mentions: List[str] = field(default_factory=list)
    competitor_moves: List[str] = field(default_factory=list)
    community_discussion: List[str] = field(default_factory=list)


@dataclass
class ParadigmPrediction:
    id: str
    paradigm_type: ParadigmType
    paradigm_name: str
    description: str
    probability: float
    confidence: PredictionConfidence
    timeline: Timeline
    signals: Signals = field(default_factory=Signals)
    impact_score: float = 0.0
    created_at: int = 0
    status: str = "pending"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("id required")
        self.paradigm_type = ParadigmType(self.paradigm_type)
        self.confidence = PredictionConfidence(self.confidence)
        if not 0.0 <= float(self.probability) <= 1.0:
            raise ValueError("probability must be in [0, 1]")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["paradigm_type"] = self.paradigm_type.value
        d["confidence"] = self.confidence.value
        d["timeline"]["critical_window"] = list(self.timeline.critical_window)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParadigmPrediction":
        d = dict(d)
        d["timeline"] = Timeline(**d["timeline"])
        d["signals"] = Signals(**d.get("signals", {}))
        return cls(**d)


@dataclass
class CommitmentProof:
    """What gets published at commit time. The salt is NOT in here: it stays
    with the committer until reveal, so the commitment hides the prediction."""
    proof_id: str
    prediction_id: str
    prediction_hash: str
    reasoning_hash: str
    commitment: str
    confidence_weight: float
    committed_at: int
    anchor_event_id: Optional[str] = None  # Portent 30009 event id, if anchored

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AccuracyMetrics:
    total_predictions: int = 0
    correct_predictions: int = 0
    accuracy_percentage: float = 0.0
    average_lead_time: float = 0.0  # days, commit -> validation
    false_positives: int = 0
    false_negatives: int = 0
    precision: Optional[float] = None
    recall: Optional[float] = None


@dataclass
class EmergentPattern:
    pattern_id: str
    pattern_type: str
    description: str
    first_observed: int
    last_observed: int
    frequency: int
    related_predictions: List[str]
    signal_strength: float


@dataclass
class LearningCycle:
    cycle_id: str
    predictions_made: int
    predictions_correct: int
    average_error_margin: float
    lessons_learned: List[str]
    timestamp: int


@dataclass
class OracleMetrics:
    total_predictions_lifetime: int = 0
    predictions_proven_correct: int = 0
    average_lead_time_days: float = 0.0
    current_accuracy: float = 0.0
    commitments: int = 0
