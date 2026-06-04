from typing import List, Optional
from dataclasses import dataclass

try:
    from app.utils.market_regime import get_current_regime, classify_regime, kelly_fraction, Regime
except ImportError:
    async def get_current_regime():
        return {"vix": None, "regime": "normal", "kelly_multiplier": 0.8}
    def classify_regime(vix):
        return "normal"
    def kelly_fraction(edge, odds, regime, max_fraction=0.05):
        return round(min(edge / odds if odds > 0 else 0, max_fraction), 4)


@dataclass
class EdgeResult:
    ticker: str
    title: str
    category: str
    market_yes_price: float
    model_yes_prob: float
    edge_score: float
    kelly: float
    regime: str
    close_time: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "category": self.category,
            "market_yes_price": self.market_yes_price,
            "model_yes_prob": round(self.model_yes_prob * 100, 1),
            "edge_score": round(self.edge_score, 4),
            "kelly_fraction": self.kelly,
            "regime": self.regime,
            "close_time": self.close_time,
        }


class ScannerRegistry:
    async def run_all(self) -> List[dict]:
        regime_data = await get_current_regime()
        regime = regime_data.get("regime", "normal")
        all_results: List[EdgeResult] = []
        # Wire your PMBOT scanners here:
        # from scripts.wti_scanner import scan_wti_markets
        # results = await scan_wti_markets()
        # all_results.extend([EdgeResult(...) for r in results])
        return sorted(
            [r.to_dict() for r in all_results],
            key=lambda r: abs(r["edge_score"]),
            reverse=True,
        )
