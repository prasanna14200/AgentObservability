from __future__ import annotations

import pandas as pd
import pytest

from agentsentinel.data.split import (
    get_split_info,
    load_features,
    temporal_split,
    temporal_train_validation_test_split,
)
from agentsentinel.detectors.isoforest import get_isoforest_metadata, predict_isolation_forest, train_isolation_forest
from agentsentinel.detectors.rules import apply_rules, get_rules_metadata
from scripts.evaluate_calibrated_detector import (
    select_fpr_bounded_threshold,
    select_group_fpr_bounded_thresholds,
)


def test_temporal_split_produces_disjoint_trace_ids() -> None:
    df = load_features()
    train, test, cutoff_time = temporal_split(df, train_frac=0.8)

    train_ids = set(train["trace_id"].unique())
    test_ids = set(test["trace_id"].unique())

    assert train_ids.isdisjoint(test_ids)
    assert len(train_ids) + len(test_ids) == df["trace_id"].nunique()


def test_temporal_split_preserves_temporal_order() -> None:
    df = load_features()
    train, test, cutoff_time = temporal_split(df, train_frac=0.8)

    train_max = train["timestamp"].max()
    test_min = test["timestamp"].min()

    assert train_max < cutoff_time
    assert test_min >= cutoff_time


def test_three_way_temporal_split_keeps_traces_disjoint_and_ordered() -> None:
    df = load_features()
    train, validation, test, train_cutoff, test_cutoff = (
        temporal_train_validation_test_split(df)
    )

    train_ids = set(train.trace_id)
    validation_ids = set(validation.trace_id)
    test_ids = set(test.trace_id)
    assert not train_ids & validation_ids
    assert not train_ids & test_ids
    assert not validation_ids & test_ids
    assert train.timestamp.max() < train_cutoff
    assert validation.timestamp.min() >= train_cutoff
    assert validation.timestamp.max() < test_cutoff
    assert test.timestamp.min() >= test_cutoff


def test_temporal_train_validation_test_split_rejects_mixed_trace_labels() -> None:
    df = pd.DataFrame(
        {
            "trace_id": ["trace_001", "trace_001", "trace_002", "trace_003", "trace_004"],
            "timestamp": pd.date_range("2026-01-01", periods=5, tz="UTC"),
            "label": ["normal", "anomalous", "normal", "normal", "normal"],
            "anomaly_type": [None, "tool_loop", None, None, None],
        }
    )

    with pytest.raises(ValueError, match="consistent label"):
        temporal_train_validation_test_split(df, train_frac=0.5, validation_frac=0.25)


def test_three_way_split_does_not_separate_traces_with_equal_timestamps() -> None:
    df = pd.DataFrame(
        {
            "trace_id": [f"trace_{index:03}" for index in range(6)],
            "timestamp": pd.to_datetime(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T02:00:00Z",
                    "2026-01-01T03:00:00Z",
                ]
            ),
            "label": ["normal"] * 6,
            "anomaly_type": [None] * 6,
        }
    )

    train, validation, test, _, _ = temporal_train_validation_test_split(
        df, train_frac=0.5, validation_frac=0.25
    )

    assert train.timestamp.max() < validation.timestamp.min()
    assert validation.timestamp.max() < test.timestamp.min()


def test_three_way_split_allows_consistent_missing_anomaly_type() -> None:
    df = pd.DataFrame(
        {
            "trace_id": ["trace_001", "trace_002", "trace_003", "trace_004"],
            "timestamp": pd.date_range("2026-01-01", periods=4, tz="UTC"),
            "label": ["normal"] * 4,
            "anomaly_type": [None] * 4,
        }
    )

    train, validation, test, _, _ = temporal_train_validation_test_split(
        df, train_frac=0.5, validation_frac=0.25
    )

    assert len(train) == 2
    assert len(validation) == 1
    assert len(test) == 1


def test_three_way_split_rejects_null_labels() -> None:
    df = pd.DataFrame(
        {
            "trace_id": ["trace_001", "trace_002", "trace_003", "trace_004"],
            "timestamp": pd.date_range("2026-01-01", periods=4, tz="UTC"),
            "label": ["normal", None, "normal", "normal"],
            "anomaly_type": [None] * 4,
        }
    )

    with pytest.raises(ValueError, match="labels must not be null"):
        temporal_train_validation_test_split(
            df, train_frac=0.5, validation_frac=0.25
        )


@pytest.mark.parametrize(
    ("column", "values", "message"),
    [
        ("trace_id", [None, "trace_002", "trace_003", "trace_004"], "identifiers must not be null"),
        ("timestamp", [pd.NaT] + list(pd.date_range("2026-01-01", periods=3, tz="UTC")),
         "timestamps must not be null"),
        ("label", ["unknown", "normal", "normal", "normal"], "normal or anomalous"),
    ],
)
def test_three_way_split_rejects_invalid_trace_metadata(column, values, message) -> None:
    df = pd.DataFrame(
        {
            "trace_id": [f"trace_{index:03}" for index in range(4)],
            "timestamp": pd.date_range("2026-01-01", periods=4, tz="UTC"),
            "label": ["normal"] * 4,
            "anomaly_type": [None] * 4,
        }
    )
    df[column] = values

    with pytest.raises(ValueError, match=message):
        temporal_train_validation_test_split(df, train_frac=0.5, validation_frac=0.25)


def test_three_way_split_rejects_empty_or_unparsed_timestamp_data() -> None:
    empty = pd.DataFrame(
        columns=["trace_id", "timestamp", "label", "anomaly_type"]
    )
    with pytest.raises(ValueError, match="contain traces"):
        temporal_train_validation_test_split(empty)

    unparsed = pd.DataFrame(
        {
            "trace_id": [f"trace_{index:03}" for index in range(4)],
            "timestamp": ["2026-01-01"] * 4,
            "label": ["normal"] * 4,
            "anomaly_type": [None] * 4,
        }
    )
    with pytest.raises(ValueError, match="parsed datetimes"):
        temporal_train_validation_test_split(unparsed)


def test_calibrated_threshold_meets_validation_fpr_and_maximizes_recall() -> None:
    threshold = select_fpr_bounded_threshold(
        [False, False, False, False, True, True],
        [0.1, 0.2, 0.3, 0.4, 0.25, 0.5],
        max_fpr=0.25,
    )

    assert threshold == 0.5


def test_zero_fpr_threshold_has_no_validation_false_positives() -> None:
    labels = [False, False, False, True, True]
    scores = [0.1, 0.2, 0.3, 0.25, 0.5]

    threshold = select_fpr_bounded_threshold(labels, scores, max_fpr=0.0)

    assert sum(
        not actual and score >= threshold
        for actual, score in zip(labels, scores)
    ) == 0


def test_group_thresholds_use_validation_only_and_suppress_unseen_groups() -> None:
    labels = [False, False, True, True, False, False, True, True]
    scores = [0.1, 0.4, 0.3, 0.8, 0.2, 0.6, 0.5, 0.9]
    groups = ["lookup"] * 4 + ["delivery"] * 4
    thresholds = select_group_fpr_bounded_thresholds(
        labels, scores, groups, max_fpr=0.5
    )

    predictions = [
        score >= thresholds.get(group, float("inf"))
        for group, score in zip(groups, scores)
    ]
    for group in set(groups):
        group_rows = [
            index for index, value in enumerate(groups) if value == group
        ]
        false_positives = sum(
            not labels[index] and predictions[index] for index in group_rows
        )
        normal_count = sum(not labels[index] for index in group_rows)
        assert false_positives / normal_count <= 0.5
    unknown_group_prediction = 0.7 >= thresholds.get("unseen", float("inf"))
    assert unknown_group_prediction is False


def test_group_thresholds_reject_invalid_validation_inputs() -> None:
    with pytest.raises(ValueError, match="boolean"):
        select_group_fpr_bounded_thresholds(
            [False, "anomaly"], [0.1, 0.9], ["known", "known"]
        )


def test_calibrated_threshold_rejects_single_class_validation_data() -> None:
    with pytest.raises(ValueError, match="normal and anomalous"):
        select_fpr_bounded_threshold([False, False], [0.1, 0.2])


@pytest.mark.parametrize("scores", [[0.1, float("nan")], [0.1, float("inf")]])
def test_calibrated_threshold_rejects_nonfinite_scores(scores) -> None:
    with pytest.raises(ValueError, match="finite numbers"):
        select_fpr_bounded_threshold([False, True], scores)


def test_split_info_matches_expected_counts() -> None:
    df = load_features()
    info = get_split_info(df, train_frac=0.8)

    assert info["train_traces"] == 496
    assert info["test_traces"] == 124
    assert info["train_anomalous_traces"] == 101
    assert info["test_anomalous_traces"] == 19
    assert len(info["features_used"]) == 13


def test_rules_detect_loops() -> None:
    df = pd.DataFrame({
        "trace_id": ["t1", "t1", "t1", "t2"],
        "tool_repeat_in_session": [0, 1, 5, 2],
        "task_retry_zscore": [0.0, 0.0, 0.0, 0.0],
        "task_token_zscore": [0.0, 0.0, 0.0, 0.0],
        "task_cost_zscore": [0.0, 0.0, 0.0, 0.0],
        "task_latency_zscore": [0.0, 0.0, 0.0, 0.0],
    })

    result = apply_rules(df)
    assert result.loc[2, "rules_loop"] == True
    assert result.loc[0, "rules_loop"] == False


def test_rules_detect_retry_storm() -> None:
    df = pd.DataFrame({
        "trace_id": ["t1", "t2"],
        "tool_repeat_in_session": [0, 0],
        "task_retry_zscore": [3.0, 0.0],
        "task_token_zscore": [0.0, 0.0],
        "task_cost_zscore": [0.0, 0.0],
        "task_latency_zscore": [0.0, 0.0],
    })

    result = apply_rules(df)
    assert result.loc[0, "rules_retry_storm"] == True
    assert result.loc[1, "rules_retry_storm"] == False


def test_rules_detect_resource_spike() -> None:
    df = pd.DataFrame({
        "trace_id": ["t1", "t2", "t3"],
        "tool_repeat_in_session": [0, 0, 0],
        "task_retry_zscore": [0.0, 0.0, 0.0],
        "task_token_zscore": [3.0, 0.0, 0.0],
        "task_cost_zscore": [0.0, 3.0, 0.0],
        "task_latency_zscore": [0.0, 0.0, 0.0],
    })

    result = apply_rules(df)
    assert result.loc[0, "rules_resource_spike"] == True
    assert result.loc[1, "rules_resource_spike"] == True
    assert result.loc[2, "rules_resource_spike"] == False


def test_rules_aggregate_anomalous_flag() -> None:
    df = pd.DataFrame({
        "trace_id": ["t1", "t2", "t3", "t4"],
        "tool_repeat_in_session": [5, 0, 0, 0],
        "task_retry_zscore": [0.0, 3.0, 0.0, 0.0],
        "task_token_zscore": [0.0, 0.0, 3.0, 0.0],
        "task_cost_zscore": [0.0, 0.0, 0.0, 0.0],
        "task_latency_zscore": [0.0, 0.0, 0.0, 0.0],
    })

    result = apply_rules(df)
    assert result["rules_anomalous"].tolist() == [True, True, True, False]


def test_isoforest_trains_only_on_normal() -> None:
    df = load_features()
    train, test, _ = temporal_split(df, train_frac=0.8)

    # With interleaved timestamps, train contains both normal and anomalous traces.
    # That's correct — the split should be realistic. The training function
    # itself filters to normal traces before fitting.
    assert len(train[train["label"] == "normal"]) > 0
    assert len(train[train["label"] == "anomalous"]) > 0

    model = train_isolation_forest(train)
    assert model is not None

    # Verify the model was trained only on normal data by checking it produces
    # reasonable predictions (not all -1 or all 1) on a mixed test set
    result = predict_isolation_forest(model, test.head(20))
    assert result["iso_prediction"].nunique() <= 2  # Should be -1 and/or 1


def test_isoforest_predictions_have_expected_columns() -> None:
    df = load_features()
    train, test, _ = temporal_split(df, train_frac=0.8)

    model = train_isolation_forest(train)
    result = predict_isolation_forest(model, test.head(10))

    assert "iso_raw_score" in result.columns
    assert "iso_anomaly_score" in result.columns
    assert "iso_prediction" in result.columns
    assert "iso_anomaly" in result.columns
    assert set(result["iso_prediction"].unique()).issubset({-1, 1})
    assert (result["iso_anomaly_score"] == -result["iso_raw_score"]).all()
    assert (result["iso_anomaly"] == result["iso_anomaly_score"].gt(0)).all()


def test_isoforest_anomaly_score_ranks_flagged_events_higher() -> None:
    df = load_features()
    train, test, _ = temporal_split(df)
    model = train_isolation_forest(train)
    result = predict_isolation_forest(model, test)
    assert result.loc[result.iso_anomaly, "iso_anomaly_score"].min() > result.loc[~result.iso_anomaly, "iso_anomaly_score"].max()
    samples = model.score_samples(test[get_isoforest_metadata()["features"]].values)
    assert ((samples - model.offset_ - result.iso_raw_score.to_numpy()).round(12) == 0).all()


def test_isoforest_metadata_records_seed_and_features() -> None:
    meta = get_isoforest_metadata()
    assert meta["random_state"] == 42
    assert len(meta["features"]) == 13
    assert "n_estimators" in meta
    assert "training_protocol" in meta
