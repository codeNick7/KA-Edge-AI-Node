"""A transformer, built from first principles, small enough to read in one sitting.

Why this exists: the contest requires real trained-neural-network inference, running
on-device, distinguishing three soil-condition states (stable / waterlogged_risk /
soil_health_degrading) from four scalar features (rsmi, light_delta, gas_index_trend,
rsmi_trend — see edge_node/features.py). A single self-attention block over those four
features (plus a learned [CLS] token) is already a genuine transformer encoder; scaling
it up would mostly add overfitting risk on this trial's data, not real capacity (see
docs/plan/nn-inference-upgrade-plan.md).

This module deliberately does NOT depend on torch/jax/autograd. Every gradient here is
derived and coded by hand — slower to write than `loss.backward()`, but it's the whole
point of using this as a learning aid alongside "Inference Engineering": you can see
every matrix that flows through the network and exactly how error propagates back
through it. `check_gradients()` at the bottom cross-checks every analytic gradient
against a numerical (finite-difference) one — the standard way anyone hand-rolling
backprop convinces themselves it's actually correct, not just plausible-looking.

Architecture (pre-LayerNorm transformer block, the same ordering GPT-2 popularized):

    4 features --> per-feature bin embedding + slot embedding --> 5 tokens ([CLS] + 4)
                --> LayerNorm --> single-head self-attention --> residual add
                --> LayerNorm --> 2-layer feed-forward (ReLU)  --> residual add
                --> take the [CLS] token's final vector
                --> linear layer --> softmax over 3 classes

Every scalar feature becomes a TOKEN, not a raw number fed straight into a dense layer —
that's what makes this a transformer application rather than just an MLP with extra
steps: the model can learn to attend differently to, say, rsmi vs. gas_index_trend
depending on what the *other* features currently look like, rather than treating them
as an unordered fixed-width vector.
"""

from __future__ import annotations

import numpy as np

FEATURE_NAMES = ["rsmi", "light_delta", "gas_index_trend", "rsmi_trend"]
N_FEATURES = len(FEATURE_NAMES)
N_TOKENS = N_FEATURES + 1  # +1 for the prepended [CLS] token

# Sorted, like sklearn's .classes_ convention — index order here is the model's output
# order everywhere (logits, softmax probabilities, export). "sensor_fault" is
# deliberately absent: the validity gate (edge_node/validity.py) catches that
# independently, before this model ever runs — see module docstring in trained_model.py.
CLASS_NAMES = ["soil_health_degrading", "stable", "waterlogged_risk"]
N_CLASSES = len(CLASS_NAMES)


# ── Feature binning (turns each continuous feature into a token id) ────────────────────
def compute_bin_edges(values: np.ndarray, n_bins: int) -> np.ndarray:
    """Data-driven quantile bin edges for one feature column — n_bins - 1 interior
    edges, so `np.searchsorted(edges, x)` returns a bin id in [0, n_bins - 1].
    Quantile (not equal-width) binning matters here: gas_index_trend/rsmi_trend are
    heavily concentrated near 0 with long tails (a stable pot barely moves), so
    equal-width bins would waste most of the vocabulary on the empty tails.
    """
    quantiles = np.linspace(0.0, 1.0, n_bins + 1)[1:-1]
    edges = np.quantile(values, quantiles)
    return np.unique(edges)  # duplicate edges (heavily repeated values) would create empty bins


def quantize(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.searchsorted(edges, values, side="right").astype(np.int64)


def quantize_row(features: dict[str, float], bin_edges: dict[str, np.ndarray]) -> list[int]:
    """Same as quantize(), but for a single reading's features dict — this is the
    exact function export_transformer.py's generated code re-implements dependency-free
    on the board, so the two must stay in lockstep (round-trip-tested, see
    test_export_transformer.py)."""
    return [
        int(np.searchsorted(bin_edges[name], features[name], side="right"))
        for name in FEATURE_NAMES
    ]


# ── Small numpy building blocks, each with a matching *_backward ───────────────────────
def layernorm_forward(x: np.ndarray, gamma: np.ndarray, beta: np.ndarray, eps: float = 1e-5):
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    inv_std = 1.0 / np.sqrt(var + eps)
    x_hat = (x - mean) * inv_std
    out = gamma * x_hat + beta
    cache = (x_hat, inv_std, gamma)
    return out, cache


def layernorm_backward(dout: np.ndarray, cache) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Standard LayerNorm backward — see e.g. Ba et al. 2016, appendix. `d` is the
    feature dimension being normalized over (the last axis)."""
    x_hat, inv_std, gamma = cache
    d = x_hat.shape[-1]
    dgamma = np.sum(dout * x_hat, axis=tuple(range(dout.ndim - 1)))
    dbeta = np.sum(dout, axis=tuple(range(dout.ndim - 1)))
    dx_hat = dout * gamma
    dx = (1.0 / d) * inv_std * (
        d * dx_hat
        - np.sum(dx_hat, axis=-1, keepdims=True)
        - x_hat * np.sum(dx_hat * x_hat, axis=-1, keepdims=True)
    )
    return dx, dgamma, dbeta


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


class TinyTransformerClassifier:
    """A 1-block, 1-head transformer encoder + linear classification head.

    Parameters live in a flat dict (`self.params`) rather than a class-per-layer object
    graph — deliberately, so training (`fit`), gradient checking, and export can all
    just iterate `self.params.items()` instead of needing bespoke plumbing per layer.
    """

    def __init__(self, n_bins: int, d_model: int = 8, d_ff: int = 16, seed: int = 0):
        self.n_bins = n_bins
        self.d_model = d_model
        self.d_ff = d_ff
        rng = np.random.default_rng(seed)

        def small(*shape):
            # Small init matters more than usual here: with only ~1,100 total
            # parameters and no dropout/warmup, a too-large init makes the softmax
            # saturate immediately and gradients vanish before training starts.
            return (rng.standard_normal(shape) * 0.1).astype(np.float64)

        self.params = {
            # One embedding table per feature (NOT shared) — rsmi's bin 0 and
            # light_delta's bin 0 mean unrelated things, so they must not share a
            # learned vector.
            **{f"emb_{name}": small(n_bins, d_model) for name in FEATURE_NAMES},
            "cls_token": small(1, d_model),
            "slot_emb": small(N_TOKENS, d_model),  # "which position" embedding
            "ln1_gamma": np.ones(d_model), "ln1_beta": np.zeros(d_model),
            "Wq": small(d_model, d_model), "Wk": small(d_model, d_model),
            "Wv": small(d_model, d_model), "Wo": small(d_model, d_model),
            "ln2_gamma": np.ones(d_model), "ln2_beta": np.zeros(d_model),
            "W1": small(d_model, d_ff), "b1": np.zeros(d_ff),
            "W2": small(d_ff, d_model), "b2": np.zeros(d_model),
            "Wc": small(d_model, N_CLASSES), "bc": np.zeros(N_CLASSES),
        }

    # -- forward -------------------------------------------------------------------
    def forward(self, X_bins: np.ndarray):
        """X_bins: (N, N_FEATURES) integer bin ids. Returns (logits, cache)."""
        p = self.params
        N = X_bins.shape[0]
        d = self.d_model

        feat_tokens = np.stack(
            [p[f"emb_{name}"][X_bins[:, i]] for i, name in enumerate(FEATURE_NAMES)],
            axis=1,
        )  # (N, N_FEATURES, d)
        cls = np.broadcast_to(p["cls_token"], (N, 1, d))
        tokens = np.concatenate([cls, feat_tokens], axis=1) + p["slot_emb"][None, :, :]  # (N, N_TOKENS, d)

        ln1_out, ln1_cache = layernorm_forward(tokens, p["ln1_gamma"], p["ln1_beta"])

        Q = ln1_out @ p["Wq"]
        K = ln1_out @ p["Wk"]
        V = ln1_out @ p["Wv"]
        scores = (Q @ K.transpose(0, 2, 1)) / np.sqrt(d)  # (N, N_TOKENS, N_TOKENS)
        attn = softmax(scores, axis=-1)
        attn_out = (attn @ V) @ p["Wo"]
        resid1 = tokens + attn_out

        ln2_out, ln2_cache = layernorm_forward(resid1, p["ln2_gamma"], p["ln2_beta"])
        h_pre = ln2_out @ p["W1"] + p["b1"]
        h = np.maximum(h_pre, 0.0)  # ReLU
        ffn_out = h @ p["W2"] + p["b2"]
        resid2 = resid1 + ffn_out

        cls_final = resid2[:, 0, :]  # (N, d) — only the [CLS] token feeds the head
        logits = cls_final @ p["Wc"] + p["bc"]

        cache = dict(
            X_bins=X_bins, feat_tokens=feat_tokens, tokens=tokens,
            ln1_out=ln1_out, ln1_cache=ln1_cache, Q=Q, K=K, V=V, attn=attn, attn_out=attn_out,
            resid1=resid1, ln2_out=ln2_out, ln2_cache=ln2_cache, h_pre=h_pre, h=h,
            ffn_out=ffn_out, resid2=resid2, cls_final=cls_final,
        )
        return logits, cache

    # -- loss ------------------------------------------------------------------------
    @staticmethod
    def cross_entropy(logits: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
        """y: (N,) int class ids. Returns (mean loss, dlogits) — softmax+CE's
        combined gradient collapses to (probs - one_hot), the one bit of calculus
        worth memorizing here."""
        probs = softmax(logits, axis=-1)
        N = logits.shape[0]
        loss = -np.mean(np.log(np.clip(probs[np.arange(N), y], 1e-12, 1.0)))
        dlogits = probs.copy()
        dlogits[np.arange(N), y] -= 1.0
        dlogits /= N
        return float(loss), dlogits

    # -- backward ----------------------------------------------------------------------
    def backward(self, cache: dict, dlogits: np.ndarray) -> dict[str, np.ndarray]:
        """Manual reverse-mode differentiation, mirroring forward() step for step in
        reverse. Every line here answers "if I nudge this intermediate value, how much
        does the loss change" — the definition of a gradient — propagated backward from
        the loss to every parameter."""
        p = self.params
        grads = {k: np.zeros_like(v) for k, v in p.items()}
        d = self.d_model
        N = cache["X_bins"].shape[0]

        # logits = cls_final @ Wc + bc
        grads["Wc"] = cache["cls_final"].T @ dlogits
        grads["bc"] = dlogits.sum(axis=0)
        dcls_final = dlogits @ p["Wc"].T  # (N, d)

        # cls_final = resid2[:, 0, :]  -> scatter gradient back into token 0 only
        dresid2 = np.zeros_like(cache["resid2"])
        dresid2[:, 0, :] = dcls_final

        # resid2 = resid1 + ffn_out
        dresid1 = dresid2.copy()
        dffn_out = dresid2

        # ffn_out = h @ W2 + b2
        grads["W2"] = cache["h"].reshape(-1, self.d_ff).T @ dffn_out.reshape(-1, d)
        grads["b2"] = dffn_out.sum(axis=(0, 1))
        dh = dffn_out @ p["W2"].T

        # h = relu(h_pre)
        dh_pre = dh * (cache["h_pre"] > 0)

        # h_pre = ln2_out @ W1 + b1
        grads["W1"] = cache["ln2_out"].reshape(-1, d).T @ dh_pre.reshape(-1, self.d_ff)
        grads["b1"] = dh_pre.sum(axis=(0, 1))
        dln2_out = dh_pre @ p["W1"].T

        # ln2_out = LayerNorm(resid1)
        dresid1_from_ln2, grads["ln2_gamma"], grads["ln2_beta"] = layernorm_backward(dln2_out, cache["ln2_cache"])
        dresid1 = dresid1 + dresid1_from_ln2

        # resid1 = tokens + attn_out
        dtokens = dresid1.copy()
        dattn_out = dresid1

        # attn_out = (attn @ V) @ Wo
        attn_v = cache["attn"] @ cache["V"]  # (N, N_TOKENS, d) — recomputed, cheap
        grads["Wo"] = attn_v.reshape(-1, d).T @ dattn_out.reshape(-1, d)
        dattn_v = dattn_out @ p["Wo"].T

        dattn = dattn_v @ cache["V"].transpose(0, 2, 1)  # (N, N_TOKENS, N_TOKENS)
        dV = cache["attn"].transpose(0, 2, 1) @ dattn_v

        # attn = softmax(scores) — Jacobian-vector product for softmax over the last axis
        dscores = cache["attn"] * (dattn - np.sum(dattn * cache["attn"], axis=-1, keepdims=True))

        # scores = (Q @ K^T) / sqrt(d)
        dscores /= np.sqrt(d)
        dQ = dscores @ cache["K"]
        dK = dscores.transpose(0, 2, 1) @ cache["Q"]

        grads["Wq"] = cache["ln1_out"].reshape(-1, d).T @ dQ.reshape(-1, d)
        grads["Wk"] = cache["ln1_out"].reshape(-1, d).T @ dK.reshape(-1, d)
        grads["Wv"] = cache["ln1_out"].reshape(-1, d).T @ dV.reshape(-1, d)
        dln1_out = dQ @ p["Wq"].T + dK @ p["Wk"].T + dV @ p["Wv"].T

        # ln1_out = LayerNorm(tokens)
        dtokens_from_ln1, grads["ln1_gamma"], grads["ln1_beta"] = layernorm_backward(dln1_out, cache["ln1_cache"])
        dtokens = dtokens + dtokens_from_ln1

        # tokens = concat([cls, feat_tokens]) + slot_emb
        grads["slot_emb"] = dtokens.sum(axis=0)
        grads["cls_token"] = dtokens[:, 0, :].sum(axis=0, keepdims=True)
        dfeat_tokens = dtokens[:, 1:, :]  # (N, N_FEATURES, d)

        # feat_tokens[:, i, :] = emb_{name}[X_bins[:, i]]  -> scatter-add into the
        # rows of each embedding table that were actually looked up this batch.
        X_bins = cache["X_bins"]
        for i, name in enumerate(FEATURE_NAMES):
            np.add.at(grads[f"emb_{name}"], X_bins[:, i], dfeat_tokens[:, i, :])

        return grads

    # -- training ----------------------------------------------------------------------
    def fit(
        self, X_bins: np.ndarray, y: np.ndarray, *, epochs: int = 60, batch_size: int = 4096,
        lr: float = 0.05, seed: int = 0, verbose: bool = True,
    ) -> list[float]:
        """Plain mini-batch Adam — hand-written rather than imported, for the same
        "see every moving part" reason as the rest of this module. Adam (Kingma & Ba
        2014) instead of vanilla SGD because the loss surface here is small but not
        well-conditioned (attention scores and embeddings sit at very different
        scales); per-parameter adaptive step sizes converge far more reliably than a
        single global learning rate does for a network this shape.
        """
        rng = np.random.default_rng(seed)
        n = X_bins.shape[0]
        m = {k: np.zeros_like(v) for k, v in self.params.items()}
        v = {k: np.zeros_like(val) for k, val in self.params.items()}
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        t = 0
        history = []
        best_loss = float("inf")
        best_params = None

        for epoch in range(epochs):
            order = rng.permutation(n)
            epoch_loss = 0.0
            n_batches = 0
            for start in range(0, n, batch_size):
                idx = order[start : start + batch_size]
                logits, cache = self.forward(X_bins[idx])
                loss, dlogits = self.cross_entropy(logits, y[idx])
                grads = self.backward(cache, dlogits)

                t += 1
                for k in self.params:
                    m[k] = beta1 * m[k] + (1 - beta1) * grads[k]
                    v[k] = beta2 * v[k] + (1 - beta2) * (grads[k] ** 2)
                    m_hat = m[k] / (1 - beta1 ** t)
                    v_hat = v[k] / (1 - beta2 ** t)
                    self.params[k] -= lr * m_hat / (np.sqrt(v_hat) + eps)

                epoch_loss += loss
                n_batches += 1
            epoch_loss /= n_batches
            history.append(epoch_loss)
            if epoch_loss < best_loss:
                best_loss = epoch_loss
                best_params = {k: v.copy() for k, v in self.params.items()}
            if verbose and (epoch % 5 == 0 or epoch == epochs - 1):
                print(f"  epoch {epoch:3d}  loss={epoch_loss:.4f}")

        # Adam with a fixed step size can overshoot a good minimum late in training
        # (visible as loss ticking back up after it's already bottomed out) — keep
        # whichever epoch's weights actually had the lowest training loss, not
        # necessarily the last one. This is the same idea as early-stopping
        # checkpointing, just picking the checkpoint after the fact instead of
        # stopping training early.
        if best_params is not None:
            self.params = best_params
        return history

    def predict(self, X_bins: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        logits, _ = self.forward(X_bins)
        probs = softmax(logits, axis=-1)
        pred_idx = np.argmax(probs, axis=-1)
        confidence = probs[np.arange(len(pred_idx)), pred_idx]
        return pred_idx, confidence


def check_gradients(model: TinyTransformerClassifier, X_bins: np.ndarray, y: np.ndarray, eps: float = 1e-5) -> dict[str, float]:
    """Numerical vs. analytic gradient check — the standard way to trust hand-written
    backprop. For each parameter tensor, perturb a handful of random entries by +-eps,
    re-run the forward pass, and compare the resulting finite-difference slope against
    backward()'s value. Returns the max relative error per parameter name; anything
    above ~1e-4 indicates a real bug in backward(), not floating-point noise.
    """
    rng = np.random.default_rng(0)
    logits, cache = model.forward(X_bins)
    _, dlogits = model.cross_entropy(logits, y)
    analytic = model.backward(cache, dlogits)

    def loss_at(params):
        saved = model.params
        model.params = params
        logits, _ = model.forward(X_bins)
        loss, _ = model.cross_entropy(logits, y)
        model.params = saved
        return loss

    max_rel_err = {}
    for name, param in model.params.items():
        flat = param.reshape(-1)
        n_check = min(5, flat.size)
        positions = rng.choice(flat.size, size=n_check, replace=False)
        errs = []
        for pos in positions:
            params_plus = {k: v.copy() for k, v in model.params.items()}
            params_minus = {k: v.copy() for k, v in model.params.items()}
            params_plus[name].reshape(-1)[pos] += eps
            params_minus[name].reshape(-1)[pos] -= eps
            numeric = (loss_at(params_plus) - loss_at(params_minus)) / (2 * eps)
            analytic_val = analytic[name].reshape(-1)[pos]
            denom = max(abs(numeric), abs(analytic_val), 1e-8)
            errs.append(abs(numeric - analytic_val) / denom)
        max_rel_err[name] = max(errs)
    return max_rel_err
