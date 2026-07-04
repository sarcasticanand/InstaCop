from dataclasses import dataclass, field

HARD_CAP_INR = 30.0


@dataclass
class CostLedger:
    cap_inr: float = HARD_CAP_INR
    entries: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total_inr(self) -> float:
        return sum(cost for _, cost in self.entries)

    def add(self, label: str, cost_inr: float) -> None:
        self.entries.append((label, cost_inr))

    def over_cap(self) -> bool:
        return self.total_inr > self.cap_inr
