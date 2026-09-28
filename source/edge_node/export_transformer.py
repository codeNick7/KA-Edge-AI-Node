"""Export a fitted TinyTransformerClassifier to dependency-free Python.

Same rationale as edge_node/export.py (the decision-tree exporter): the QRB2210
should never need numpy/scipy installed just to run a few hundred already-learned
weights forward. This walks a fitted TinyTransformerClassifier's `params` dict and
`bin_edges`, and emits a plain Python module that re-implements the exact same
forward-pass math (see tiny_transformer.py's `forward()`) using nested lists and
`bisect`/`math` from the standard library only.

Correctness is round-trip-tested (test_export_transformer.py): the generated module's
`trained_predict()` output is compared against the in-memory model's `predict()` on
held-out rows and must match to floating-point tolerance — same pattern that caught a
real bug in the decision-tree exporter before it ever reached hardware.
"""

from __future__ import annotations

import datetime
import json

import numpy as np

from .tiny_transformer import N_TOKENS, TinyTransformerClassifier


def _list(arr: np.ndarray) -> list:
    """Round-tripped through a plain list of Python floats (not np.float64) so the
    generated file has no numpy types anywhere in it."""
    return np.asarray(arr).round(8).tolist()


def export_model(
    model: TinyTransformerClassifier,
    bin_edges: dict[str, np.ndarray],
    feature_names: list[str],
    class_names: list[str],
    training_rows: int,
    out_path: str,
    model_version: str | None = None,
) -> None:
    p = model.params
    version = model_version or f"tiny-transformer-{training_rows}rows-d{model.d_model}"
    weights = {
        "emb": {name: _list(p[f"emb_{name}"]) for name in feature_names},
        "cls_token": _list(p["cls_token"][0]),
        "slot_emb": _list(p["slot_emb"]),
        "ln1_gamma": _list(p["ln1_gamma"]), "ln1_beta": _list(p["ln1_beta"]),
        "Wq": _list(p["Wq"]), "Wk": _list(p["Wk"]), "Wv": _list(p["Wv"]), "Wo": _list(p["Wo"]),
        "ln2_gamma": _list(p["ln2_gamma"]), "ln2_beta": _list(p["ln2_beta"]),
        "W1": _list(p["W1"]), "b1": _list(p["b1"]), "W2": _list(p["W2"]), "b2": _list(p["b2"]),
        "Wc": _list(p["Wc"]), "bc": _list(p["bc"]),
    }
    bin_edges_out = {name: _list(edges) for name, edges in bin_edges.items()}

    header = f'''"""AUTO-GENERATED — do not hand-edit. Regenerate via scripts/train_transformer.py --export.

model_version: {version}
generated_at:  {datetime.datetime.now(datetime.timezone.utc).isoformat()}
training_rows: {training_rows}
architecture:  1-block, 1-head transformer encoder ([CLS] + {len(feature_names)} feature
               tokens, d_model={model.d_model}, d_ff={model.d_ff}) + linear classification head
               (edge_node/tiny_transformer.py)
features (in order): {feature_names!r}
classes: {class_names!r}

No runtime dependency on numpy/scipy — this is a plain Python re-implementation of the
fitted transformer's forward pass (bisect + math from the standard library only), safe
to run standalone on the QRB2210 (architecture doc Section 9).
"""

import bisect
import math

MODEL_VERSION = {version!r}
FEATURE_NAMES = {feature_names!r}
CLASS_NAMES = {class_names!r}

_BIN_EDGES = {json.dumps(bin_edges_out, indent=4)}

_W = {json.dumps(weights, indent=4)}


def _quantize(name, value):
    return bisect.bisect_right(_BIN_EDGES[name], value)


def _matvec(mat, vec):
    """mat: list of rows (len(vec) each) -> len(mat)-length output."""
    return [sum(row[i] * vec[i] for i in range(len(vec))) for row in mat]


def _vec_matrix(vec, mat):
    """vec (len n) @ mat (n x m) -> length-m output, i.e. row-vector times matrix."""
    n_out = len(mat[0])
    return [sum(vec[i] * mat[i][j] for i in range(len(vec))) for j in range(n_out)]


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _layernorm(x, gamma, beta, eps=1e-5):
    n = len(x)
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / n
    inv_std = 1.0 / math.sqrt(var + eps)
    return [gamma[i] * (x[i] - mean) * inv_std + beta[i] for i in range(n)]


def _softmax(xs):
    m = max(xs)
    exps = [math.exp(v - m) for v in xs]
    total = sum(exps)
    return [v / total for v in exps]


def trained_predict(rsmi, light_delta, gas_index_trend, rsmi_trend):
    """Same call signature as the decision-tree export it replaces — see
    edge_node/tiny_transformer.py's module docstring for the full architecture."""
    raw = dict(rsmi=rsmi, light_delta=light_delta, gas_index_trend=gas_index_trend, rsmi_trend=rsmi_trend)
    d = len(_W["cls_token"])

    # tokens[0] = [CLS], tokens[1:] = one per feature, each + its slot embedding
    feat_vecs = [_W["emb"][name][_quantize(name, raw[name])] for name in FEATURE_NAMES]
    tokens = [_W["cls_token"]] + feat_vecs
    tokens = [_add(tokens[i], _W["slot_emb"][i]) for i in range(len(tokens))]

    ln1 = [_layernorm(t, _W["ln1_gamma"], _W["ln1_beta"]) for t in tokens]
    Q = [_vec_matrix(t, _W["Wq"]) for t in ln1]
    K = [_vec_matrix(t, _W["Wk"]) for t in ln1]
    V = [_vec_matrix(t, _W["Wv"]) for t in ln1]

    scale = math.sqrt(d)
    attn_out = []
    for i in range(len(tokens)):
        scores = [sum(Q[i][k] * K[j][k] for k in range(d)) / scale for j in range(len(tokens))]
        weights = _softmax(scores)
        pooled = [sum(weights[j] * V[j][k] for j in range(len(tokens))) for k in range(d)]
        attn_out.append(_vec_matrix(pooled, _W["Wo"]))

    resid1 = [_add(tokens[i], attn_out[i]) for i in range(len(tokens))]
    ln2 = [_layernorm(t, _W["ln2_gamma"], _W["ln2_beta"]) for t in resid1]

    cls_hidden_pre = _add(_vec_matrix(ln2[0], _W["W1"]), _W["b1"])
    cls_hidden = [max(0.0, v) for v in cls_hidden_pre]  # ReLU
    cls_ffn = _add(_vec_matrix(cls_hidden, _W["W2"]), _W["b2"])
    cls_final = _add(resid1[0], cls_ffn)

    logits = _add(_vec_matrix(cls_final, _W["Wc"]), _W["bc"])
    probs = _softmax(logits)
    best_idx = max(range(len(probs)), key=lambda i: probs[i])
    return CLASS_NAMES[best_idx], round(probs[best_idx], 4)
'''

    with open(out_path, "w") as f:
        f.write(header)
