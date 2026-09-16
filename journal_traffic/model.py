"""Acceptance-rate model and submission-traffic estimation.

Design rationale
----------------
The quantity we want (annual submissions) is never published for most
journals, so it cannot be learned directly. What *is* sometimes published is
the acceptance rate. So we learn that, and divide:

    submissions = research_articles / acceptance_rate

The model is gradient boosting over ~15 journal-level features, trained on
whatever declared acceptance rates the harvester found. Three quantile
regressors (10th, 50th, 90th) give a band rather than a point estimate,
because a point estimate on this data would be false precision.

Why not a neural model: the labelled set is typically 300-3000 journals with
tabular features. Gradient boosting wins on that shape and stays inspectable,
which matters when you have to justify a journal recommendation to a client.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold

from .scopus import parse_quartile, quartile_prior

FEATURES = [
    "log_articles",
    "articles_growth",
    "citescore",
    "quartile",
    "two_yr_mean_citedness",
    "h_index",
    "is_oa",
    "log_apc",
    "special_issue_share",
    "intl_author_share",
    "top_country_share",
    "days_to_first_decision",
    "articles_per_year_volatility",
]


def build_features(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)

    counts = df["research_works_by_year"].apply(lambda d: sorted(d.items()))
    latest = counts.apply(lambda c: c[-1][1] if c else 0)
    earliest = counts.apply(lambda c: c[0][1] if c else 0)

    df["articles_latest"] = latest
    latest = pd.to_numeric(latest, errors="coerce").fillna(0).astype(float)
    earliest = pd.to_numeric(earliest, errors="coerce").fillna(0).astype(float)
    df["log_articles"] = np.log1p(latest)
    df["articles_growth"] = np.where(
        earliest > 0, (latest - earliest) / earliest, 0.0
    )
    df["articles_per_year_volatility"] = counts.apply(
        lambda c: float(np.std([v for _, v in c])) if len(c) > 1 else 0.0
    )
    df["quartile"] = df["quartile"].apply(parse_quartile)

    # Proxy quartile when the Scopus spreadsheets are absent. Ranks journals
    # against each other on 2-year mean citedness. This is NOT the Scopus
    # quartile — it is a within-batch percentile that lets the tier-D prior
    # produce a band instead of nothing. Flagged so the output can say so.
    missing = df["quartile"].isna()
    # A quartile that came from SCImago is real — never overwrite it with the
    # within-batch proxy.
    if "quartile_source" in df.columns:
        missing = missing & df["quartile_source"].isna()
    df["quartile_is_proxy"] = False
    if missing.any():
        cite = pd.to_numeric(df["two_yr_mean_citedness"], errors="coerce")
        if cite.notna().sum() >= 4:
            ranks = cite.rank(pct=True)
            proxy = pd.cut(ranks, [0, .25, .5, .75, 1.0], labels=[4, 3, 2, 1])
            fill = missing & proxy.notna()
            df.loc[fill, "quartile"] = proxy[fill].astype(float)
            df.loc[fill, "quartile_is_proxy"] = True
    df["is_oa"] = pd.to_numeric(df["is_oa"], errors="coerce").fillna(0).astype(float)
    # OpenAlex returns apc_usd as None for most subscription journals. When
    # every journal in a batch is None the column stays object dtype, and
    # numpy ufuncs reject the Python ints that fillna leaves behind. Coerce
    # explicitly rather than relying on pandas inferring a numeric type.
    df["log_apc"] = np.log1p(
        pd.to_numeric(df["apc_usd"], errors="coerce").fillna(0).astype(float)
    )

    for col in FEATURES:
        if col not in df:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


@dataclass
class ModelReport:
    n_train: int
    cv_mae: float
    baseline_mae: float
    feature_importance: dict[str, float]
    conformal_delta: float = 0.0

    def summary(self) -> str:
        lift = (self.baseline_mae - self.cv_mae) / self.baseline_mae * 100
        top = sorted(self.feature_importance.items(), key=lambda kv: -kv[1])[:5]
        lines = [
            f"Trained on {self.n_train} journals with declared acceptance rates.",
            f"Cross-validated MAE: {self.cv_mae:.3f} acceptance-rate points "
            f"(quartile-prior baseline {self.baseline_mae:.3f}, {lift:+.0f}%).",
            f"Bands widened to +/-{self.conformal_delta:.3f} where the quantile "
            f"models were over-confident.",
            "Top features: " + ", ".join(f"{k} ({v:.2f})" for k, v in top),
        ]
        if self.n_train < 150:
            lines.append(
                "WARNING: small training set. Treat tier-C predictions as "
                "indicative only and prefer tier A/B journals when routing."
            )
        if lift < 10:
            lines.append(
                "WARNING: the model barely beats the quartile prior. The "
                "declared-rate harvest is probably too thin or too biased."
            )
        return "\n".join(lines)


class AcceptanceModel:
    """Quantile gradient boosting → acceptance-rate band."""

    QUANTILES = (0.1, 0.5, 0.9)

    def __init__(self):
        self.models: dict[float, GradientBoostingRegressor] = {}
        self.report: ModelReport | None = None
        # Split-conformal widening. Quantile GBR fits its 10th/90th on the
        # training residuals and comes out over-confident on held-out data —
        # measured 80% bands were containing the truth about two thirds of
        # the time. The out-of-fold residuals say how wrong the median is on
        # data it has not seen, and the band is widened to at least that.
        self.conformal_delta: float = 0.0

    def _make(self, q: float) -> GradientBoostingRegressor:
        return GradientBoostingRegressor(
            loss="quantile",
            alpha=q,
            n_estimators=300,
            max_depth=3,
            learning_rate=0.05,
            min_samples_leaf=8,
            subsample=0.9,
            random_state=42,
        )

    def fit(self, df: pd.DataFrame) -> ModelReport:
        train = df[df["declared_acceptance_rate"].notna()].copy()
        if len(train) < 15:
            raise ValueError(
                f"Only {len(train)} journals with declared acceptance rates. "
                "Need 15+. Options: raise --limit to widen the pool, or supply "
                "a labels CSV with --labels (issn,rate) from journals whose "
                "real rates you know — 20 of your own beats 200 scraped."
            )

        X = train[FEATURES].to_numpy(dtype=float)
        y = train["declared_acceptance_rate"].to_numpy(dtype=float)
        X = np.nan_to_num(X, nan=-1.0)

        # CV on the median model only.
        kf = KFold(n_splits=max(2, min(5, len(train) // 5)), shuffle=True,
                   random_state=42)
        preds = np.zeros_like(y)
        for tr, te in kf.split(X):
            m = self._make(0.5).fit(X[tr], y[tr])
            preds[te] = m.predict(X[te])
        cv_mae = mean_absolute_error(y, preds)

        # 80th percentile of out-of-fold absolute error: the half-width an
        # 80% interval needs if the quantile models under-cover.
        self.conformal_delta = float(np.quantile(np.abs(y - preds), 0.8))

        # Baseline: predict the quartile prior midpoint.
        base = []
        for _, r in train.iterrows():
            band = quartile_prior(r.get("quartile"), r.get("subject_area"))
            base.append(np.mean(band) if band else y.mean())
        baseline_mae = mean_absolute_error(y, base)

        for q in self.QUANTILES:
            self.models[q] = self._make(q).fit(X, y)

        imp = dict(
            zip(FEATURES, self.models[0.5].feature_importances_.round(3).tolist())
        )
        self.report = ModelReport(len(train), cv_mae, baseline_mae, imp,
                                  self.conformal_delta)
        return self.report

    @property
    def is_fitted(self) -> bool:
        return all(q in self.models for q in self.QUANTILES)

    def predict_band(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.is_fitted:
            raise RuntimeError(
                "AcceptanceModel.predict_band called before a successful fit(). "
                "Callers must set their model reference to None when fit() "
                "raises, rather than keeping the unfitted instance."
            )
        X = np.nan_to_num(df[FEATURES].to_numpy(dtype=float), nan=-1.0)
        lo = np.clip(self.models[0.1].predict(X), 0.02, 0.95)
        mid = np.clip(self.models[0.5].predict(X), 0.02, 0.95)
        hi = np.clip(self.models[0.9].predict(X), 0.02, 0.95)
        # Quantile models can cross on small data; enforce ordering.
        lo, hi = np.minimum(lo, mid), np.maximum(hi, mid)

        d = getattr(self, "conformal_delta", 0.0)
        if d > 0:
            lo = np.clip(np.minimum(lo, mid - d), 0.02, 0.95)
            hi = np.clip(np.maximum(hi, mid + d), 0.02, 0.95)
        return pd.DataFrame({"ar_lo": lo, "ar_mid": mid, "ar_hi": hi}, index=df.index)

    def save(self, path: Path) -> None:
        import joblib

        joblib.dump({"models": self.models, "delta": self.conformal_delta}, path)
        if self.report:
            path.with_suffix(".report.json").write_text(
                json.dumps(asdict(self.report), indent=2)
            )

    @classmethod
    def load(cls, path: Path) -> "AcceptanceModel":
        import joblib

        obj = cls()
        blob = joblib.load(path)
        if isinstance(blob, dict) and "models" in blob:
            obj.models, obj.conformal_delta = blob["models"], blob.get("delta", 0.0)
        else:                                    # a model saved before widening
            obj.models = blob
        return obj


def estimate_traffic(df: pd.DataFrame, model: AcceptanceModel | None) -> pd.DataFrame:
    """Combine evidence tiers into a submissions estimate per journal.

    Tier A: journal states submissions received      → use directly
    Tier B: journal states acceptance rate           → articles / rate
    Tier C: model prediction                         → articles / predicted band
    Tier D: quartile prior only                      → wide band, low confidence
    """
    out = df.copy()
    bands = (
        model.predict_band(out)
        if model is not None and getattr(model, "is_fitted", False)
        else None
    )

    tiers, lows, mids, highs, rates = [], [], [], [], []

    for i, row in out.iterrows():
        arts = row["articles_latest"]

        if pd.notna(row.get("declared_submissions")):
            n = float(row["declared_submissions"])
            tiers.append("A")
            lows.append(n)
            mids.append(n)
            highs.append(n)
            rates.append(arts / n if n else np.nan)
            continue

        if pd.notna(row.get("declared_acceptance_rate")):
            r = float(row["declared_acceptance_rate"])
            tiers.append("B")
            # ±15% relative uncertainty on a declared rate (rounding, year drift)
            lows.append(arts / (r * 1.15))
            mids.append(arts / r)
            highs.append(arts / (r * 0.85))
            rates.append(r)
            continue

        if bands is not None:
            b = bands.loc[i]
            tiers.append("C")
            lows.append(arts / b["ar_hi"])
            mids.append(arts / b["ar_mid"])
            highs.append(arts / b["ar_lo"])
            rates.append(b["ar_mid"])
            continue

        prior = quartile_prior(row.get("quartile"), row.get("subject_area"))
        if prior:
            lo_r, hi_r = prior
            tiers.append("D")
            lows.append(arts / hi_r)
            mids.append(arts / np.mean(prior))
            highs.append(arts / lo_r)
            rates.append(np.mean(prior))
        else:
            tiers.append("-")
            lows.append(np.nan)
            mids.append(np.nan)
            highs.append(np.nan)
            rates.append(np.nan)

    out["evidence_tier"] = tiers
    out["acceptance_rate_used"] = np.round(rates, 3)
    def _int_series(values):
        return pd.Series(values, index=out.index).round().astype("Int64")

    out["submissions_low"] = _int_series(lows)
    out["submissions_est"] = _int_series(mids)
    out["submissions_high"] = _int_series(highs)

    # Regular-track competition: special issues absorb slots that never
    # competed against a normal submission.
    si = out["special_issue_share"].fillna(0)
    out["regular_track_articles"] = np.round(out["articles_latest"] * (1 - si))
    out["regular_track_odds"] = np.where(
        out["submissions_est"].notna() & (out["submissions_est"] > 0),
        np.round(out["regular_track_articles"] / out["submissions_est"], 3),
        np.nan,
    )
    out["submissions_per_month"] = _int_series(out["submissions_est"] / 12)
    return out
