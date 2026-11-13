from paper_portfolio import PaperPortfolio
from strategy import Opportunity


SIDE_LABELS = {"UP": "Up", "DOWN": "Down", "PAIR": "Up+Down"}


class PaperTrader:
    """Print screenshot-style fills and mirror them into the paper ledger."""

    def __init__(self, portfolio: PaperPortfolio):
        self.portfolio = portfolio

    def execute(self, opportunity: Opportunity) -> bool:
        if opportunity.sell:
            return self._execute_sell(opportunity)
        if opportunity.side == "PAIR":
            return self._execute_pair(opportunity)
        cost = self.portfolio.record_trade(opportunity)
        if cost <= 0:
            return False
        label = SIDE_LABELS[opportunity.side]
        print(
            f"[PAPER] BUY {label} @ {opportunity.ask:.2f} "
            f"x {opportunity.quantity:.1f} (~${cost:.2f}) - "
            f"{opportunity.strategy} {label}@{opportunity.ask:.3f}",
            flush=True,
        )
        return True

    def _execute_sell(self, opportunity: Opportunity) -> bool:
        proceeds = self.portfolio.record_sell(opportunity)
        if proceeds <= 0:
            return False
        label = SIDE_LABELS[opportunity.side]
        print(
            f"[PAPER] SELL {label} @ {opportunity.ask:.2f} "
            f"x {opportunity.quantity:.1f} (~${proceeds:.2f}) - "
            f"rebal sell_{opportunity.side.lower()}@{opportunity.ask:.2f} "
            f"{opportunity.condition}".rstrip(),
            flush=True,
        )
        return True

    def _execute_pair(self, opportunity: Opportunity) -> bool:
        cost = self.portfolio.record_trade(opportunity)
        if cost <= 0:
            return False
        for label, ask, unit_cost in (
            ("Up", opportunity.up_ask, opportunity.up_cost_per_share),
            ("Down", opportunity.down_ask, opportunity.down_cost_per_share),
        ):
            if ask is None:
                continue
            leg_cost = opportunity.quantity * unit_cost
            print(
                f"[PAPER] BUY {label} @ {ask:.2f} "
                f"x {opportunity.quantity:.1f} (~${leg_cost:.2f}) - "
                f"arb {label} comb={opportunity.comb_ratio:.3f}@{ask:.3f}",
                flush=True,
            )
        return True
