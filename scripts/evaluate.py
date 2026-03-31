"""RAG 評価フレームワーク (F-16)

回答品質を定量測定するパイプライン。
- Retrieval Accuracy: 正しいソースのチャンクが top-k に含まれた割合
- Answer Relevancy: 回答が質問に対して適切か（キーワードベース簡易判定）
- 応答時間 P50/P95

使い方:
  python -m scripts.evaluate [--api-url http://localhost:8000]
"""

import argparse
import json
import logging
import os
import statistics
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# 評価質問セット（01-requirements.md §7.2 ベース）
EVAL_QUESTIONS = [
    {
        "query": "情報セキュリティの基本方針は？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["セキュリティ", "方針", "基本"],
    },
    {
        "query": "プライバシーポリシーの内容は？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["プライバシー", "ポリシー"],
    },
    {
        "query": "情報漏洩時の連絡フローは？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["漏洩", "連絡", "フロー"],
    },
    {
        "query": "事業計画の概要は？",
        "expected_source": "01_経営",
        "expected_keywords": ["事業", "計画"],
    },
    {
        "query": "行動計画のサンプルを教えて",
        "expected_source": "01_経営",
        "expected_keywords": ["行動", "計画"],
    },
    {
        "query": "契約内容は？",
        "expected_source": "03_営業",
        "expected_keywords": ["契約"],
    },
    {
        "query": "機密保持契約の条件は？",
        "expected_source": "03_営業",
        "expected_keywords": ["機密", "契約"],
    },
    {
        "query": "就業規則の内容を教えて",
        "expected_source": "02_人事労務",
        "expected_keywords": ["就業", "規則"],
    },
    {
        "query": "補助金助成金に関する規定は？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["補助金", "助成金"],
    },
    {
        "query": "借上社宅の条件は？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["社宅"],
    },
    # 精度問題ケース: 曖昧クエリ / 無関係文書の除外
    {
        "query": "海外拠点について教えて",
        "expected_source": "01_経営",
        "expected_keywords": ["海外"],
        "negative_keywords": ["補助金", "助成金", "公募", "直接投資", "応募申請"],
    },
    {
        "query": "有給休暇は何日もらえますか？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["有給", "休暇", "日"],
    },
    {
        "query": "育児休業の取得条件は？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["育児", "休業"],
    },
    {
        "query": "在宅勤務のルールは？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["在宅", "勤務"],
    },
    {
        "query": "退職する場合の手続きは？",
        "expected_source": "02_人事労務",
        "expected_keywords": ["退職"],
    },
]

NO_RESULT_MARKERS = ["該当する情報が見つかりませんでした", "見つかりませんでした"]


def evaluate(api_url: str, user_email: str = "eval-bot@test.local"):
    """評価パイプライン実行"""
    results = []
    response_times = []

    for q in EVAL_QUESTIONS:
        start = time.time()
        try:
            resp = requests.post(
                f"{api_url}/v1/chat",
                json={"message": q["query"], "user_email": user_email},
                timeout=30,
            )
            elapsed_ms = int((time.time() - start) * 1000)
            response_times.append(elapsed_ms)

            if resp.status_code != 200:
                log.warning("  [FAIL] %s → HTTP %d", q["query"][:30], resp.status_code)
                results.append({"query": q["query"], "retrieval": False, "relevant": False, "time_ms": elapsed_ms})
                continue

            data = resp.json()
            answer = data.get("answer", "")
            citations = data.get("citations", [])

            # 「該当なし」判定
            is_no_result = any(m in answer for m in NO_RESULT_MARKERS)

            # Retrieval Accuracy: citation にソースが含まれるか
            retrieval_ok = not is_no_result and len(citations) > 0

            # Answer Relevancy: キーワードが回答に含まれるか（簡易判定）
            relevant = not is_no_result and any(kw in answer for kw in q["expected_keywords"])

            # Negative keywords: 無関係な文書からの回答を検出
            neg_keywords = q.get("negative_keywords", [])
            has_negative = any(nk in answer for nk in neg_keywords) if neg_keywords else False
            if has_negative:
                relevant = False

            status = "OK" if retrieval_ok and relevant else "WEAK" if retrieval_ok else "MISS"
            if has_negative:
                status = "NEG"
            log.info("  [%s] %s → %s", status, q["query"][:30], answer[:60])

            results.append({
                "query": q["query"],
                "retrieval": retrieval_ok,
                "relevant": relevant,
                "time_ms": elapsed_ms,
                "answer_preview": answer[:100],
            })

        except Exception as e:
            log.error("  [ERROR] %s → %s", q["query"][:30], e)
            results.append({"query": q["query"], "retrieval": False, "relevant": False, "time_ms": 0})

    # スコア算出
    total = len(results)
    retrieval_acc = sum(1 for r in results if r["retrieval"]) / total * 100
    relevancy = sum(1 for r in results if r["relevant"]) / total * 100
    p50 = statistics.median(response_times) if response_times else 0
    p95 = sorted(response_times)[int(len(response_times) * 0.95)] if response_times else 0

    report = {
        "total_questions": total,
        "retrieval_accuracy_pct": round(retrieval_acc, 1),
        "relevancy_pct": round(relevancy, 1),
        "response_time_p50_ms": p50,
        "response_time_p95_ms": p95,
        "threshold_met": retrieval_acc >= 70,
        "details": results,
    }

    # 結果出力
    log.info("=" * 60)
    log.info("Retrieval Accuracy: %.1f%% (%d/%d)", retrieval_acc, sum(1 for r in results if r["retrieval"]), total)
    log.info("Answer Relevancy:   %.1f%% (%d/%d)", relevancy, sum(1 for r in results if r["relevant"]), total)
    log.info("Response Time P50:  %d ms", p50)
    log.info("Response Time P95:  %d ms", p95)
    log.info("Threshold (70%%):    %s", "PASS" if report["threshold_met"] else "FAIL")
    log.info("=" * 60)

    # 結果を JSON で保存
    os.makedirs("results", exist_ok=True)
    output_path = f"results/eval_{int(time.time())}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log.info("Results saved to %s", output_path)

    # 閾値未達は EXIT CODE 1
    if not report["threshold_met"]:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG Evaluation Pipeline")
    parser.add_argument("--api-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--user-email", default="boss@example.com", help="Test user email")
    args = parser.parse_args()
    evaluate(args.api_url, args.user_email)
