"""Export a fitted sklearn DecisionTreeClassifier to dependency-free Python.

Why this exists: shipping the full scikit-learn/numpy runtime onto the
QRB2210 just to evaluate a shallow tree is unnecessary weight for a device
meant to run unattended for years (architecture doc Section 9). A decision
tree's *learned* logic is just nested threshold comparisons — this walks
the fitted tree structure (tree_.feature/threshold/children_left/right/value)
and generates a plain Python function with no runtime dependencies at all.

This is a generic exporter — it does not assume the fitted tree matches the
PRD Section 6 tree's exact shape (rsmi < 0.20, etc.). Whatever splits
training actually learns get exported as-is.

Also records model_version and provenance in the generated file's header,
per the architecture doc's evidence-pipeline requirement (Section 8: model
version recorded alongside each inference).
"""

from __future__ import annotations

import datetime
from importlib.metadata import version as _pkg_version

from sklearn.tree import DecisionTreeClassifier
from sklearn.tree import _tree


def _leaf_confidence(value_row) -> float:
    total = sum(value_row)
    if total == 0:
        return 0.5
    return max(value_row) / total


def _emit_node(tree, node_id: int, feature_names: list[str], class_names, indent: int) -> list[str]:
    pad = "    " * indent
    if tree.feature[node_id] == _tree.TREE_UNDEFINED:
        value = tree.value[node_id][0]
        class_idx = int(max(range(len(value)), key=lambda i: value[i]))
        label = str(class_names[class_idx])
        confidence = _leaf_confidence(value)
        return [f"{pad}return {label!r}, {confidence:.4f}"]

    feature = feature_names[tree.feature[node_id]]
    threshold = tree.threshold[node_id]
    lines = [f"{pad}if {feature} <= {threshold:.6f}:"]
    lines += _emit_node(tree, tree.children_left[node_id], feature_names, class_names, indent + 1)
    lines.append(f"{pad}else:")
    lines += _emit_node(tree, tree.children_right[node_id], feature_names, class_names, indent + 1)
    return lines


def sklearn_tree_to_python_source(
    clf: DecisionTreeClassifier,
    feature_names: list[str],
    model_version: str,
    n_training_rows: int,
) -> str:
    """Generate a standalone Python module source string from a fitted tree."""
    class_names = [str(c) for c in clf.classes_]
    signature = ", ".join(feature_names)
    body = _emit_node(clf.tree_, 0, feature_names, class_names, indent=1)
    body_src = "\n".join(body)

    header = f'''"""AUTO-GENERATED — do not hand-edit. Regenerate via edge_node.train --export.

model_version: {model_version}
generated_at:  {datetime.datetime.now(datetime.timezone.utc).isoformat()}
sklearn_version_at_export: {_pkg_version("scikit-learn")}
training_rows: {n_training_rows}
features (in order): {feature_names}
classes: {class_names}

No runtime dependency on scikit-learn/numpy — this is a plain nested
if/else translation of the fitted tree, safe to run standalone on the
QRB2210 (architecture doc Section 9).
"""

MODEL_VERSION = {model_version!r}

'''
    func = f"def trained_predict({signature}):\n" + body_src + "\n"
    return header + func


def export_model(
    clf: DecisionTreeClassifier,
    feature_names: list[str],
    output_path: str,
    model_version: str,
    n_training_rows: int,
) -> None:
    source = sklearn_tree_to_python_source(clf, feature_names, model_version, n_training_rows)
    with open(output_path, "w") as f:
        f.write(source)
