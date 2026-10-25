import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


class DixonColesModel:
    """Dixon-Coles model with exponential time weighting."""

    def __init__(self, half_life_days: float = 365.0, max_goals: int = 8):
        self.half_life_days = half_life_days
        self.max_goals = max_goals
        self.teams_: list[str] = []
        self.params_: dict | None = None

    @staticmethod
    def _tau(home_goals: int, away_goals: int, lambda_home: float, lambda_away: float, rho: float) -> float:
        if home_goals == 0 and away_goals == 0:
            return 1 - lambda_home * lambda_away * rho
        if home_goals == 0 and away_goals == 1:
            return 1 + lambda_home * rho
        if home_goals == 1 and away_goals == 0:
            return 1 + lambda_away * rho
        if home_goals == 1 and away_goals == 1:
            return 1 - rho
        return 1.0

    def _negative_log_likelihood(self, params: np.ndarray, matches: list[dict]) -> float:
        attack = params[: len(self.teams_)]
        defence = params[len(self.teams_) : 2 * len(self.teams_)]
        home_advantage, rho = params[-2], params[-1]
        team_index = {team: i for i, team in enumerate(self.teams_)}
        total = 0.0

        for match in matches:
            i, j = team_index[match["home"]], team_index[match["away"]]
            lambda_home = min(np.exp(attack[i] + defence[j] + home_advantage), 15.0)
            lambda_away = min(np.exp(attack[j] + defence[i]), 15.0)
            log_p_home = poisson.logpmf(match["home_goals"], lambda_home)
            log_p_away = poisson.logpmf(match["away_goals"], lambda_away)
            tau = self._tau(match["home_goals"], match["away_goals"], lambda_home, lambda_away, rho)
            if tau <= 0:
                return 1e12
            total -= match["weight"] * (log_p_home + log_p_away + np.log(tau))

        return total

    def fit(self, matches: list[dict]) -> "DixonColesModel":
        if not matches:
            raise ValueError("No matches provided")

        self.teams_ = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
        n_teams = len(self.teams_)
        latest_date = max(m["date"] for m in matches)

        for match in matches:
            age_days = (latest_date - match["date"]).days
            match["weight"] = 0.5 ** (age_days / self.half_life_days)

        initial = np.concatenate([
            np.zeros(n_teams),
            np.zeros(n_teams),
            [0.25, -0.05],
        ])

        result = minimize(
            self._negative_log_likelihood,
            initial,
            args=(matches,),
            method="L-BFGS-B",
            options={"maxiter": 500},
        )

        self.params_ = {
            "attack": dict(zip(self.teams_, result.x[:n_teams])),
            "defence": dict(zip(self.teams_, result.x[n_teams : 2 * n_teams])),
            "home_advantage": float(result.x[-2]),
            "rho": float(result.x[-1]),
        }
        return self

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        if self.params_ is None:
            raise RuntimeError("Model must be fitted first")
        lambda_home = np.exp(
            self.params_["attack"][home]
            + self.params_["defence"][away]
            + self.params_["home_advantage"]
        )
        lambda_away = np.exp(
            self.params_["attack"][away] + self.params_["defence"][home]
        )
        return float(lambda_home), float(lambda_away)

    def score_matrix(self, home: str, away: str) -> np.ndarray:
        lambda_home, lambda_away = self.expected_goals(home, away)
        home_probs = poisson.pmf(np.arange(self.max_goals + 1), lambda_home)
        away_probs = poisson.pmf(np.arange(self.max_goals + 1), lambda_away)
        matrix = np.outer(home_probs, away_probs)

        for hg in range(2):
            for ag in range(2):
                tau = self._tau(hg, ag, lambda_home, lambda_away, self.params_["rho"])
                matrix[hg, ag] *= tau

        return matrix / matrix.sum()

    def predict(self, home: str, away: str) -> dict:
        matrix = self.score_matrix(home, away)
        home_win = float(np.tril(matrix, -1).sum())
        draw = float(np.trace(matrix))
        away_win = float(np.triu(matrix, 1).sum())
        over_25 = float(sum(matrix[h, a] for h in range(matrix.shape[0]) for a in range(matrix.shape[1]) if h + a > 2))
        btts = float(sum(matrix[h, a] for h in range(1, matrix.shape[0]) for a in range(1, matrix.shape[1])))
        flat = [(float(matrix[h, a]), h, a) for h in range(matrix.shape[0]) for a in range(matrix.shape[1])]
        top_scores = sorted(flat, reverse=True)[:5]

        return {
            "home": home,
            "away": away,
            "expected_goals": dict(zip(("home", "away"), self.expected_goals(home, away))),
            "probabilities": {
                "home_win": home_win,
                "draw": draw,
                "away_win": away_win,
                "over_2_5": over_25,
                "both_teams_to_score": btts,
            },
            "top_scores": [
                {"home_goals": s[1], "away_goals": s[2], "probability": s[0]}
                for s in top_scores
            ],
        }
