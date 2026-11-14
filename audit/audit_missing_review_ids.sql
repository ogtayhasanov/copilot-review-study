-- Run this entire SELECT in BigQuery, using your billing project.
-- 50 randomly sampled missing review IDs, seed 20261112, plus 10 known-present controls.
-- Raw presence uses only date, event type, and review ID. NO reviewer or heading filter.
-- Zero rows cannot be mistaken for success: the final result always has 60 target rows.
-- The rule diagnostics emulate export_copilot_verdicts.sql; API headings may have changed later.
WITH targets AS (
  SELECT 'missing' AS sample_kind, review_id FROM UNNEST([
      '5228525527',
      '5561702538',
      '5225336026',
      '5411985249',
      '5269802271',
      '5796535426',
      '5172283946',
      '5262750440',
      '5451196918',
      '5393774953',
      '5202007787',
      '5490670134',
      '5648214727',
      '5225815199',
      '5246882429',
      '5530735726',
      '5449098880',
      '5453093181',
      '5112102635',
      '5590898686',
      '5331932973',
      '5153333017',
      '5611476557',
      '5173931225',
      '5379696791',
      '5647647942',
      '5105366827',
      '5166564622',
      '5365429728',
      '5213067111',
      '5435949660',
      '5407547842',
      '5309147211',
      '5124451015',
      '5125935299',
      '5435405790',
      '5293819907',
      '5195997735',
      '5448836457',
      '5132249245',
      '5224670239',
      '5696799978',
      '5141106447',
      '5375192543',
      '5784971595',
      '5436928450',
      '5280310939',
      '5245756933',
      '5297436660',
      '5235480523'
  ]) AS review_id
  UNION ALL
  SELECT 'positive_control', review_id FROM UNNEST([
      '5173719779',
      '5110648580',
      '5126580195',
      '5272822866',
      '5108384897',
      '5461823637',
      '5120618530',
      '5672603070',
      '5126761207',
      '5123570544'
  ]) AS review_id
), raw AS (
  SELECT CAST(id AS STRING) AS event_id, repo.name AS repo,
    COALESCE(JSON_VALUE(payload, '$.pull_request.number'), JSON_VALUE(payload, '$.number')) AS pr,
    JSON_VALUE(payload, '$.review.id') AS review_id,
    actor.login AS actor,
    JSON_VALUE(payload, '$.action') AS action,
    LOWER(COALESCE(JSON_VALUE(payload, '$.review.user.login'), actor.login)) AS reviewer,
    JSON_VALUE(payload, '$.review.submitted_at') AS native_submitted_at,
    COALESCE(SAFE_CAST(JSON_VALUE(payload, '$.review.submitted_at') AS TIMESTAMP), created_at) AS review_time,
    created_at AS event_time,
    LOWER(REGEXP_EXTRACT(JSON_VALUE(payload, '$.review.body'),
      r'(?im)^\s*#{2,4}\s*(?:🟢\s*|🟡\s*|🔵\s*)?(Approval recommended|Changes recommended|Needs a closer look)\b')) AS heading
  FROM `githubarchive.day.2026*`
  WHERE _TABLE_SUFFIX BETWEEN '0901' AND '1109'
    AND type = 'PullRequestReviewEvent'
    AND JSON_VALUE(payload, '$.review.id') IN (SELECT review_id FROM targets)
), raw_summary AS (
  SELECT review_id, COUNT(*) AS raw_event_count,
    MIN(event_time) AS first_archive_event, MAX(event_time) AS last_archive_event,
    STRING_AGG(DISTINCT repo, ', ') AS archive_repositories,
    STRING_AGG(DISTINCT actor, ', ') AS actors,
    STRING_AGG(DISTINCT reviewer, ', ') AS reviewers,
    STRING_AGG(DISTINCT action, ', ') AS actions,
    STRING_AGG(DISTINCT heading, ', ') AS headings_seen,
    COUNTIF(action IN ('created','updated')) AS events_passing_action_rule,
    COUNTIF(reviewer IN ('copilot','copilot-pull-request-reviewer','copilot-pull-request-reviewer[bot]')) AS events_passing_reviewer_rule,
    COUNTIF(heading IS NOT NULL) AS events_passing_heading_rule
  FROM raw GROUP BY review_id
), originally_selected AS (
  SELECT * FROM raw
  WHERE action IN ('created','updated')
    AND review_time >= TIMESTAMP '2026-09-01 00:00:00+00'
    AND review_time < TIMESTAMP '2026-11-10 00:00:00+00'
    AND pr IS NOT NULL AND reviewer IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY repo,review_id
    ORDER BY IF(action='created',0,1),event_time,event_id
  )=1
), selection_summary AS (
  SELECT review_id,
    COUNTIF(reviewer IN ('copilot','copilot-pull-request-reviewer','copilot-pull-request-reviewer[bot]')
            AND heading IS NOT NULL) AS rows_original_rules_would_emit,
    STRING_AGG(CONCAT(repo, ' | ', COALESCE(action,'NULL'), ' | ', COALESCE(reviewer,'NULL'),
                      ' | ', COALESCE(heading,'NO MATCHING HEADING')), '; ') AS selected_versions
  FROM originally_selected GROUP BY review_id
)
SELECT t.sample_kind, t.review_id,
  COALESCE(r.raw_event_count,0) AS raw_event_count,
  CASE WHEN r.raw_event_count IS NULL THEN 'NOT_FOUND_IN_SCANNED_ARCHIVE_WINDOW'
       WHEN COALESCE(s.rows_original_rules_would_emit,0)>0 THEN 'PRESENT_AND_ORIGINAL_RULES_WOULD_EMIT'
       ELSE 'PRESENT_BUT_ORIGINAL_RULES_WOULD_DROP' END AS diagnostic,
  r.* EXCEPT(review_id,raw_event_count),
  COALESCE(s.rows_original_rules_would_emit,0) AS rows_original_rules_would_emit,
  s.selected_versions,
  COUNTIF(t.sample_kind='missing' AND r.raw_event_count>0) OVER() AS missing_ids_found_out_of_50,
  COUNTIF(t.sample_kind='positive_control' AND r.raw_event_count>0) OVER() AS controls_found_out_of_10
FROM targets t LEFT JOIN raw_summary r USING(review_id)
LEFT JOIN selection_summary s USING(review_id)
ORDER BY t.sample_kind,t.review_id;
