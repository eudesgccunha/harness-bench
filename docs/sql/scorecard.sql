-- Scorecard queries over the reference schema (ADR 0011). Each query starts with a `-- name:` line and ends with a
-- semicolon; they run on SQLite and PostgreSQL. A target is `harness/model`. Unknown stays unknown: a cost is NULL
-- when any trial of the target has no cost (as in the markdown report), never 0, and it sorts last, never as the
-- cheapest target (`ORDER BY x IS NULL, x`: SQLite sorts NULL first).
--
-- Every query aggregates over ALL runs in the database. To score one run, add `WHERE run_id = ?` before the `GROUP BY` of a query
-- (in `pass_hat_k`, inside the subquery).

-- name: pass_rate
-- Share of trials that passed, per target.
SELECT target_id,
       COUNT(*) AS trials,
       SUM(passed) AS passed,
       1.0 * SUM(passed) / COUNT(*) AS pass_rate
FROM trials
GROUP BY target_id
ORDER BY pass_rate DESC, target_id;

-- name: pass_hat_k
-- pass^k with k equal to the number of reps of a task: the share of tasks that passed on every rep, per target.
SELECT target_id,
       COUNT(*) AS tasks,
       SUM(all_passed) AS tasks_always_passed,
       1.0 * SUM(all_passed) / COUNT(*) AS pass_hat_k
FROM (
    SELECT target_id, task_id, MIN(passed) AS all_passed
    FROM trials
        GROUP BY target_id, task_id
) AS per_task
GROUP BY target_id
ORDER BY pass_hat_k DESC, target_id;

-- name: cost_per_success
-- Total cost divided by solved trials, per target. NULL when any trial has an unknown cost or nothing was solved.
SELECT target_id,
       SUM(passed) AS solved,
       CASE WHEN COUNT(cost_usd) = COUNT(*) THEN SUM(cost_usd) END AS total_cost_usd,
       CASE WHEN COUNT(cost_usd) = COUNT(*) AND SUM(passed) > 0 THEN SUM(cost_usd) / SUM(passed) END AS cost_per_success
FROM trials
GROUP BY target_id
ORDER BY cost_per_success IS NULL, cost_per_success, target_id;

-- Joining the spend logs of the gateway (not run by the tests; it needs a `spend_logs` table that you load from
-- LiteLLM, with one row per request and the request tag as `request_tag`). `trials.run_tag` is the tag the harness
-- sent in `x-litellm-tags` (`BENCH_TAG`), so the spend of a trial is the sum of the rows with its tag:
--
--   SELECT t.trial_id, t.target_id, SUM(s.spend) AS gateway_cost_usd
--   FROM trials AS t
--   JOIN spend_logs AS s ON s.request_tag = t.run_tag
--   GROUP BY t.trial_id, t.target_id;
