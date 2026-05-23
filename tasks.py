"""
tasks.py — الـ Celery tasks اللي بتعمل الـ processing الحقيقي.

كل task بتشتغل في background worker منفصل عن Flask.
Flask بيبعت الـ task ويرد على الـ user فوراً بـ task_id،
والـ user بيعمل polling عشان يعرف الـ progress.
"""

import os
import json
import traceback
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from celery_app import celery

RESULTS_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "static", "results"))
os.makedirs(RESULTS_FOLDER, exist_ok=True)



def update_progress(task, step: str, percent: int, meta: dict = None):
    """بتبعت الـ progress للـ Redis عشان Flask يقدر يقرأها."""
    state_meta = {"step": step, "percent": percent}
    if meta:
        state_meta.update(meta)
    task.update_state(state="PROGRESS", meta=state_meta)


# ─── Main Processing Task ─────────────────────────────────────────────────────

@celery.task(bind=True, name="tasks.process_dataset")
def process_dataset(self, job_id: str, file_path: str, operations: list):
    """
    الـ task الرئيسية — بتعمل كل الـ processing على الداتا.

    Parameters:
        job_id     : رقم الـ job في الـ database
        file_path  : المسار الكامل للـ CSV
        operations : list من الـ operations اللي اختارها الـ user
                     ["stats", "cleaning", "ml", "charts"]
    """
    results = {}

    try:
        update_progress(self, "Reading dataset...", 5)

        df = pd.read_csv(file_path)
        original_shape = df.shape
        results["overview"] = {
            "rows":        int(original_shape[0]),
            "columns":     int(original_shape[1]),
            "column_names": df.columns.tolist(),
            "dtypes":      {col: str(dtype) for col, dtype in df.dtypes.items()},
            "memory_usage_kb": round(df.memory_usage(deep=True).sum() / 1024, 2),
        }

        # ── Step 2: Data Cleaning ─────────────────────────────────────────
        update_progress(self, "Cleaning data...", 20)

        cleaning_report = {}
        missing_before  = int(df.isnull().sum().sum())
        duplicates      = int(df.duplicated().sum())

        df = df.drop_duplicates()
        cleaning_report["duplicates_removed"] = duplicates

        numeric_cols     = df.select_dtypes(include=[np.number]).columns.tolist()
        categorical_cols = df.select_dtypes(include=["object"]).columns.tolist()

        for col in numeric_cols:
            if df[col].isnull().any():
                median_val = df[col].median()
                df[col]    = df[col].fillna(median_val)

        for col in categorical_cols:
            if df[col].isnull().any():
                mode_val = df[col].mode()[0] if not df[col].mode().empty else "Unknown"
                df[col]  = df[col].fillna(mode_val)

        missing_after = int(df.isnull().sum().sum())
        cleaning_report["missing_before"]  = missing_before
        cleaning_report["missing_after"]   = missing_after
        cleaning_report["missing_filled"]  = missing_before - missing_after
        cleaning_report["rows_after_clean"]= int(len(df))

        if "cleaning" in operations:
            results["cleaning"] = cleaning_report

        # ── Step 3: Statistical Analysis ──────────────────────────────────
        if "stats" in operations:
            update_progress(self, "Running statistical analysis...", 40)

            stats = {}
            if numeric_cols:
                desc = df[numeric_cols].describe().round(4)
                stats["descriptive"] = desc.to_dict()

                if len(numeric_cols) > 1:
                    corr = df[numeric_cols].corr().round(4)
                    stats["correlation"] = corr.to_dict()

                    corr_pairs = []
                    for i, c1 in enumerate(numeric_cols):
                        for c2 in numeric_cols[i+1:]:
                            val = round(float(corr.loc[c1, c2]), 4)
                            corr_pairs.append({"col1": c1, "col2": c2, "correlation": val})
                    corr_pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
                    stats["top_correlations"] = corr_pairs[:5]

            results["stats"] = stats

        # ── Step 4: Charts ─────────────────────────────────────────────────
        if "charts" in operations:
            update_progress(self, "Generating charts...", 60)

            charts = []
            chart_folder = os.path.join(RESULTS_FOLDER, job_id)
            os.makedirs(chart_folder, exist_ok=True)

            for col in numeric_cols[:4]:
                fig, ax = plt.subplots(figsize=(7, 4))
                ax.hist(df[col].dropna(), bins=30, color="#3B8BD4", edgecolor="#1a1a2e", alpha=0.85)
                ax.set_title(f"Distribution of {col}", color="white", fontsize=13)
                ax.set_xlabel(col, color="#aaa")
                ax.set_ylabel("Frequency", color="#aaa")
                ax.tick_params(colors="#aaa")
                fig.patch.set_facecolor("#0d1117")
                ax.set_facecolor("#161b22")
                for spine in ax.spines.values():
                    spine.set_edgecolor("#30363d")
                plt.tight_layout()
                fname = f"hist_{col.replace(' ','_')}.png"
                plt.savefig(os.path.join(chart_folder, fname), dpi=100, bbox_inches="tight")
                plt.close()
                charts.append({"type": "histogram", "column": col, "file": f"{job_id}/{fname}"})

            # Correlation heatmap
            if len(numeric_cols) > 1:
                corr_data = df[numeric_cols].corr()
                fig, ax   = plt.subplots(figsize=(8, 6))
                im = ax.imshow(corr_data.values, cmap="coolwarm", vmin=-1, vmax=1)
                ax.set_xticks(range(len(corr_data.columns)))
                ax.set_yticks(range(len(corr_data.columns)))
                ax.set_xticklabels(corr_data.columns, rotation=45, ha="right", color="#aaa", fontsize=9)
                ax.set_yticklabels(corr_data.columns, color="#aaa", fontsize=9)
                plt.colorbar(im, ax=ax)
                ax.set_title("Correlation Heatmap", color="white", fontsize=13)
                fig.patch.set_facecolor("#0d1117")
                ax.set_facecolor("#161b22")
                plt.tight_layout()
                fname = "correlation_heatmap.png"
                plt.savefig(os.path.join(chart_folder, fname), dpi=100, bbox_inches="tight")
                plt.close()
                charts.append({"type": "heatmap", "column": "all", "file": f"{job_id}/{fname}"})

            results["charts"] = charts

        # ── Step 5: Machine Learning ───────────────────────────────────────
        if "ml" in operations and len(numeric_cols) >= 2:
            update_progress(self, "Training ML model...", 75)

            from sklearn.ensemble import RandomForestRegressor
            from sklearn.model_selection import train_test_split
            from sklearn.preprocessing import StandardScaler
            from sklearn.metrics import mean_squared_error, r2_score

            ml_results = {}

            target_col  = numeric_cols[-1]
            feature_cols= numeric_cols[:-1]

            X = df[feature_cols].values
            y = df[target_col].values

            if len(X) >= 20:
                X_train, X_test, y_train, y_test = train_test_split(
                    X, y, test_size=0.2, random_state=42
                )
                scaler  = StandardScaler()
                X_train = scaler.fit_transform(X_train)
                X_test  = scaler.transform(X_test)

                model = RandomForestRegressor(n_estimators=100, random_state=42)
                model.fit(X_train, y_train)
                y_pred = model.predict(X_test)

                r2   = round(float(r2_score(y_test, y_pred)), 4)
                rmse = round(float(np.sqrt(mean_squared_error(y_test, y_pred))), 4)

                # Feature importance
                importances = [
                    {"feature": col, "importance": round(float(imp), 4)}
                    for col, imp in zip(feature_cols, model.feature_importances_)
                ]
                importances.sort(key=lambda x: x["importance"], reverse=True)

                ml_results = {
                    "target_column":  target_col,
                    "feature_columns":feature_cols,
                    "model":          "RandomForestRegressor",
                    "train_samples":  len(X_train),
                    "test_samples":   len(X_test),
                    "r2_score":       r2,
                    "rmse":           rmse,
                    "feature_importance": importances[:10],
                }

                # Feature importance chart
                chart_folder = os.path.join(RESULTS_FOLDER, job_id)
                os.makedirs(chart_folder, exist_ok=True)
                fig, ax = plt.subplots(figsize=(8, 5))
                top     = importances[:8]
                ax.barh([x["feature"] for x in top],
                        [x["importance"] for x in top],
                        color="#3B8BD4", edgecolor="#1a1a2e")
                ax.set_title(f"Feature Importance → {target_col}", color="white", fontsize=13)
                ax.tick_params(colors="#aaa")
                fig.patch.set_facecolor("#0d1117")
                ax.set_facecolor("#161b22")
                for spine in ax.spines.values():
                    spine.set_edgecolor("#30363d")
                plt.tight_layout()
                fname = "feature_importance.png"
                plt.savefig(os.path.join(chart_folder, fname), dpi=100, bbox_inches="tight")
                plt.close()

                if "charts" not in results:
                    results["charts"] = []
                results["charts"].append({
                    "type": "feature_importance", "column": target_col,
                    "file": f"{job_id}/{fname}"
                })
            else:
                ml_results["warning"] = "Not enough rows for ML (need at least 20)"

            results["ml"] = ml_results

        update_progress(self, "Saving results...", 90)

        chart_folder = os.path.join(RESULTS_FOLDER, job_id)
        os.makedirs(chart_folder, exist_ok=True)
        cleaned_csv  = os.path.join(chart_folder, "cleaned_data.csv")
        df.to_csv(cleaned_csv, index=False)
        results["cleaned_csv"] = f"{job_id}/cleaned_data.csv"

        summary_path = os.path.join(chart_folder, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(results, f, indent=2, default=str)

        update_progress(self, "Done!", 100)
        return {"status": "SUCCESS", "job_id": job_id, "results": results}

    except Exception as e:
        error_msg = traceback.format_exc()
        print(f"[ERROR] Task failed for job {job_id}:\n{error_msg}")
        raise self.retry(exc=e, max_retries=0)
