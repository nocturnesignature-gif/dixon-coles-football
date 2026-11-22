from datetime import date, timedelta

import numpy as np
import pytest

from src.dixon_coles import DixonColesModel


def make_matches():
    start = date(2026, 1, 1)
    results = [
        ("Alpha", "Beta", 2, 1),
        ("Beta", "Gamma", 1, 1),
        ("Gamma", "Alpha", 0, 2),
        ("Alpha", "Gamma", 3, 0),
        ("Beta", "Alpha", 1, 2),
        ("Gamma", "Beta", 2, 2),
    ]
    return [
        {
            "home": home,
            "away": away,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "date": start + timedelta(days=7 * index),
        }
        for index, (home, away, home_goals, away_goals) in enumerate(results)
    ]


@pytest.fixture(scope="module")
def fitted_model():
    model = DixonColesModel(half_life_days=180, max_goals=8)
    return model.fit(make_matches())


def test_attack_and_defence_are_normalized(fitted_model):
    attack = fitted_model.params_["attack"]
    defence = fitted_model.params_["defence"]

    assert len(attack) == 3
    assert len(defence) == 3
    assert np.isclose(sum(attack.values()), 0.0, atol=1e-8)
    assert np.isclose(sum(defence.values()), 0.0, atol=1e-8)


def test_expected_goals_are_finite_and_realistic(fitted_model):
    for home in fitted_model.params_["attack"]:
        for away in fitted_model.params_["attack"]:
            if home == away:
                continue

            lambda_home, lambda_away = fitted_model.expected_goals(home, away)

            assert np.isfinite(lambda_home)
            assert np.isfinite(lambda_away)
            assert 0.2 <= lambda_home <= 5.0
            assert 0.2 <= lambda_away <= 5.0


def test_prediction_probabilities_are_valid(fitted_model):
    prediction = fitted_model.predict("Alpha", "Beta")
    probabilities = prediction["probabilities"]

    total = (
        probabilities["home_win"]
        + probabilities["draw"]
        + probabilities["away_win"]
    )

    assert np.isclose(total, 1.0, atol=1e-8)

    for value in probabilities.values():
        assert 0.0 <= value <= 1.0


def test_score_matrix_is_normalized(fitted_model):
    matrix = fitted_model.score_matrix("Alpha", "Beta")

    assert matrix.shape == (9, 9)
    assert np.isclose(matrix.sum(), 1.0, atol=1e-8)
    assert np.all(matrix >= 0.0)


def test_predictions_are_deterministic():
    matches = make_matches()

    first = DixonColesModel(half_life_days=180, max_goals=8).fit(matches)
    second = DixonColesModel(half_life_days=180, max_goals=8).fit(matches)

    first_prediction = first.predict("Alpha", "Beta")
    second_prediction = second.predict("Alpha", "Beta")

    assert first_prediction["expected_goals"] == second_prediction["expected_goals"]
    assert first_prediction["probabilities"] == second_prediction["probabilities"]


def test_home_advantage_is_recorded(fitted_model):
    assert "home_advantage" in fitted_model.params_
    assert np.isfinite(fitted_model.params_["home_advantage"])


def test_unknown_team_is_rejected(fitted_model):
    with pytest.raises(ValueError):
        fitted_model.expected_goals("Alpha", "Unknown")


def test_unfitted_model_is_rejected():
    model = DixonColesModel()

    with pytest.raises(RuntimeError):
        model.expected_goals("Alpha", "Beta")
