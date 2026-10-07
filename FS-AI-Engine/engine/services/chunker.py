import re
from typing import List, Tuple, Set
from ..models.schemas import SpeakerTurn


class SignalTagger:
    """
    Level 1 Turn Signal Tagger.
    Processes normalized Level 1 speaker turns and identifies conversational intents:
    - Questions & Clarifications (`is_question`)
    - Commitments & Action Items (`is_commitment`)
    - Blockers, Impediments & Roadblocks (`is_blocker`)
    """

    # Interrogative regex patterns
    QUESTION_PATTERNS = [
        re.compile(r"\?\s*$"),
        re.compile(r"\b(what|why|how|who|when|where|which)\b", re.IGNORECASE),
        re.compile(r"^(can|could|would|should|is|are|do|does|did|will|have|has)\s+(you|we|it|there|anyone|anybody)\b", re.IGNORECASE),
        re.compile(r"\b(makes sense|right|correct)\s*\?", re.IGNORECASE),
        re.compile(r"\b(can you clarify|could you explain|what do you think)\b", re.IGNORECASE),
    ]

    # Commitment / Action intent patterns
    COMMITMENT_PATTERNS = [
        re.compile(r"\b(i will|i'll|we will|we'll)\b", re.IGNORECASE),
        re.compile(r"\b(i can take|i'm going to|i am going to|let me handle)\b", re.IGNORECASE),
        re.compile(r"\b(my action item|action item for me|take this on|own this)\b", re.IGNORECASE),
        re.compile(r"\b(i'll follow up|i will follow up|i'll check|i will investigate)\b", re.IGNORECASE),
        re.compile(r"\b(will send out|will deliver|will prepare|will update)\b", re.IGNORECASE),
        re.compile(r"\b(committing to|committed to|promise to)\b", re.IGNORECASE),
        re.compile(r"\b(by tomorrow|by friday|by next week|by end of day|by eod)\b", re.IGNORECASE),
    ]

    # Blocker / Roadblock patterns
    BLOCKER_PATTERNS = [
        re.compile(r"\b(blocked by|is blocking|blocker|blockers)\b", re.IGNORECASE),
        re.compile(r"\b(waiting on|waiting for|depends on|dependency on)\b", re.IGNORECASE),
        re.compile(r"\b(cannot proceed|can't proceed|stuck on|preventing us)\b", re.IGNORECASE),
        re.compile(r"\b(bottleneck|impediment|roadblock|showstopper)\b", re.IGNORECASE),
        re.compile(r"\b(running into issues|failing because|broken in prod|delayed due to)\b", re.IGNORECASE),
        re.compile(r"\b(holding us back|need approval before|gated by)\b", re.IGNORECASE),
    ]

    def detect_signals(self, text: str) -> Tuple[bool, bool, bool, List[str]]:
        """
        Evaluate text for questions, commitments, and blockers.

        Returns:
            Tuple of (is_question, is_commitment, is_blocker, signals_list)
        """
        clean_text = text.strip()
        if not clean_text:
            return False, False, False, []

        is_question = any(pattern.search(clean_text) for pattern in self.QUESTION_PATTERNS)
        is_commitment = any(pattern.search(clean_text) for pattern in self.COMMITMENT_PATTERNS)
        is_blocker = any(pattern.search(clean_text) for pattern in self.BLOCKER_PATTERNS)

        signals: List[str] = []
        if is_question:
            signals.append("question")
        if is_commitment:
            signals.append("commitment")
        if is_blocker:
            signals.append("blocker")

        return is_question, is_commitment, is_blocker, signals

    def tag_turn(self, turn: SpeakerTurn) -> SpeakerTurn:
        """Apply signal flags directly to a single SpeakerTurn instance."""
        is_q, is_c, is_b, signals = self.detect_signals(turn.text)
        turn.is_question = is_q
        turn.is_commitment = is_c
        turn.is_blocker = is_b
        turn.signals = signals
        return turn

    def tag_turns(self, turns: List[SpeakerTurn]) -> List[SpeakerTurn]:
        """Apply signal flags across a list of SpeakerTurns."""
        return [self.tag_turn(t) for t in turns]
