import ast

from sklearn.tree import DecisionTreeClassifier

from edge_node.export import sklearn_tree_to_python_source
from edge_node.train import FEATURE_NAMES, generate_synthetic_rows


def _train_small_tree(n_rows=300, seed=1):
    X, y = generate_synthetic_rows(n_rows, seed=seed)
    clf = DecisionTreeClassifier(max_depth=5, random_state=seed)
    clf.fit(X, y)
    return clf, X, y


def test_generated_source_is_valid_python():
    clf, X, y = _train_small_tree()
    source = sklearn_tree_to_python_source(clf, FEATURE_NAMES, "test-version", len(X))
    ast.parse(source)  # raises SyntaxError if malformed


def test_generated_source_has_no_numpy_or_sklearn_references():
    clf, X, y = _train_small_tree()
    source = sklearn_tree_to_python_source(clf, FEATURE_NAMES, "test-version", len(X))
    assert "np." not in source
    assert "import" not in source.split('"""', 2)[-1].split("MODEL_VERSION")[0]
    assert "sklearn" not in source.replace("sklearn_version_at_export", "")


def test_generated_predictions_match_sklearn_exactly():
    clf, X, y = _train_small_tree(n_rows=500, seed=2)
    source = sklearn_tree_to_python_source(clf, FEATURE_NAMES, "test-version", len(X))

    namespace: dict = {}
    exec(source, namespace)
    trained_predict = namespace["trained_predict"]

    sklearn_labels = clf.predict(X)
    for row, expected_label in zip(X, sklearn_labels):
        label, confidence = trained_predict(*row)
        assert label == expected_label
        assert 0.0 <= confidence <= 1.0


def test_model_version_recorded_in_header_and_constant():
    clf, X, y = _train_small_tree()
    source = sklearn_tree_to_python_source(clf, FEATURE_NAMES, "v-unit-test-123", len(X))
    assert "model_version: v-unit-test-123" in source
    assert "MODEL_VERSION = 'v-unit-test-123'" in source
